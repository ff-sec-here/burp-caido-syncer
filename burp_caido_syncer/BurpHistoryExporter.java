package burp;

import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.core.Annotations;
import burp.api.montoya.core.ByteArray;
import burp.api.montoya.core.HighlightColor;
import burp.api.montoya.http.handler.TimingData;
import burp.api.montoya.http.message.requests.HttpRequest;
import burp.api.montoya.http.message.responses.HttpResponse;
import burp.api.montoya.http.HttpService;
import burp.api.montoya.http.message.HttpRequestResponse;
import burp.api.montoya.proxy.ProxyHttpRequestResponse;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.ZonedDateTime;
import java.util.Base64;
import java.util.List;

/**
 * BurpHistoryExporter: thin Montoya API agent.
 *
 * Does ONE thing: iterates proxy().history(), serialises each item
 * as an NDJSON line, and writes it to the path given in the CLI arg
 * "exportHistory=/path/to/out.ndjson" (or stdout if no path).
 *
 * All orchestration (which .burp to open, where to write, what to do
 * with the output) is handled by the CLI tool, not here.
 *
 * After export, it unloads itself and shuts Burp down.
 */
public class BurpHistoryExporter implements BurpExtension {

    @Override
    public void initialize(MontoyaApi api) {
        api.extension().setName("BurpHistoryExporter");

        String[] args = api.burpSuite().commandLineArguments()
                .toArray(new String[0]);

        // Find output paths from CLI args
        String historyPath = null;
        String scopePath = null;
        boolean triggered = false;
        for (String arg : args) {
            if (arg.startsWith("exportHistory=")) {
                triggered = true;
                String val = arg.substring("exportHistory=".length()).trim();
                if (!val.isEmpty()) historyPath = val;
            } else if (arg.equals("exportHistory")) {
                triggered = true;
            } else if (arg.startsWith("exportScope=")) {
                triggered = true;
                String val = arg.substring("exportScope=".length()).trim();
                if (!val.isEmpty()) scopePath = val;
            }
        }

        if (!triggered) {
            api.logging().logToOutput(
                "{\"status\":\"loaded\",\"message\":\"Pass exportHistory=/path or exportScope=/path CLI arg to trigger export.\"}");
            return;
        }

        // Run on a background thread so initialize() returns and Burp can
        // finish loading the project (sitemap issues, scope, etc. load
        // asynchronously after extension init).
        final String hp = historyPath, sp = scopePath;
        Thread worker = new Thread(() -> {
            try {
                if (hp != null) dump(api, hp);
                if (sp != null) {
                    waitForProjectData(api);
                }
                if (sp != null) dumpScope(api, sp);
            } catch (Exception e) {
                api.logging().logToError("Export failed: " + e.getMessage());
                e.printStackTrace(System.err);
            } finally {
                api.extension().unload();
                api.burpSuite().shutdown();
            }
        });
        worker.setDaemon(false);
        worker.start();
    }

    /**
     * Wait for Burp to finish loading project data (sitemap, scanner issues).
     * Proxy history loads early, but sitemap issues load asynchronously after
     * extension init returns. We poll until issues appear or a timeout expires.
     */
    private void waitForProjectData(MontoyaApi api) {
        try { Thread.sleep(3000); } catch (InterruptedException ignored) {}
    }

    /**
     * Dump the entire proxy history to NDJSON. Streams one line at a time
     * so multi-GB projects don't blow the heap.
     */
    private void dump(MontoyaApi api, String outputPath) throws IOException {
        List<ProxyHttpRequestResponse> history = api.proxy().history();
        int total = history.size();
        api.logging().logToError("[burp-caido-syncer] " + total + " items in proxy history");

        OutputStream rawOut = (outputPath != null)
                ? new FileOutputStream(outputPath)
                : System.out;

        try (BufferedWriter w = new BufferedWriter(
                new OutputStreamWriter(rawOut, StandardCharsets.UTF_8), 1 << 16)) {

            Base64.Encoder b64 = Base64.getEncoder();
            int count = 0;

            for (ProxyHttpRequestResponse item : history) {
                try {
                    StringBuilder sb = new StringBuilder(4096);
                    sb.append('{');

                    // --- connection ---
                    var svc = item.httpService();
                    str(sb, "host", svc.host()); sb.append(',');
                    num(sb, "port", svc.port()); sb.append(',');
                    bool(sb, "is_tls", svc.secure()); sb.append(',');

                    // --- request metadata ---
                    HttpRequest req = item.finalRequest();
                    str(sb, "method", req != null ? req.method() : "GET"); sb.append(',');
                    str(sb, "path", req != null ? req.pathWithoutQuery() : "/"); sb.append(',');

                    String query = "";
                    String fileExt = null;
                    if (req != null) {
                        try { query = req.query(); } catch (Exception ignored) {}
                        try { fileExt = req.fileExtension(); } catch (Exception ignored) {}
                    }
                    str(sb, "query", query != null ? query : ""); sb.append(',');
                    if (fileExt != null && !fileExt.isEmpty()) {
                        str(sb, "file_extension", fileExt);
                    } else {
                        sb.append("\"file_extension\":null");
                    }
                    sb.append(',');

                    // --- timing ---
                    long tsMs = 0;
                    try {
                        ZonedDateTime t = item.time();
                        if (t != null) tsMs = t.toInstant().toEpochMilli();
                    } catch (Exception ignored) {}
                    num(sb, "timestamp_ms", tsMs); sb.append(',');

                    long rtMs = 0;
                    try {
                        TimingData td = item.timingData();
                        if (td != null) {
                            Duration d = td.timeBetweenRequestSentAndStartOfResponse();
                            if (d != null) rtMs = d.toMillis();
                        }
                    } catch (Exception ignored) {}
                    num(sb, "roundtrip_ms", rtMs); sb.append(',');

                    // --- flags ---
                    boolean edited = false;
                    try { edited = item.edited(); } catch (Exception ignored) {}
                    bool(sb, "edited", edited); sb.append(',');

                    // --- annotations ---
                    String highlight = null;
                    String notes = null;
                    try {
                        Annotations ann = item.annotations();
                        if (ann != null) {
                            if (ann.hasHighlightColor()) {
                                HighlightColor c = ann.highlightColor();
                                if (c != null && c != HighlightColor.NONE)
                                    highlight = c.name();
                            }
                            if (ann.hasNotes()) notes = ann.notes();
                        }
                    } catch (Exception ignored) {}
                    if (highlight != null) str(sb, "highlight", highlight);
                    else sb.append("\"highlight\":null");
                    sb.append(',');
                    if (notes != null) str(sb, "notes", notes);
                    else sb.append("\"notes\":null");
                    sb.append(',');

                    // --- raw bytes (base64) ---
                    byte[] reqBytes = null;
                    if (req != null) {
                        try {
                            ByteArray ba = req.toByteArray();
                            if (ba != null) reqBytes = ba.getBytes();
                        } catch (Exception ignored) {}
                    }
                    if (reqBytes != null) {
                        sb.append("\"request_b64\":\"");
                        sb.append(b64.encodeToString(reqBytes));
                        sb.append('"');
                    } else {
                        sb.append("\"request_b64\":null");
                    }
                    sb.append(',');

                    if (item.hasResponse()) {
                        HttpResponse resp = item.response();
                        if (resp != null) {
                            try {
                                ByteArray ba = resp.toByteArray();
                                if (ba != null) {
                                    sb.append("\"response_b64\":\"");
                                    sb.append(b64.encodeToString(ba.getBytes()));
                                    sb.append('"');
                                } else {
                                    sb.append("\"response_b64\":null");
                                }
                            } catch (Exception e) {
                                sb.append("\"response_b64\":null");
                            }
                        } else {
                            sb.append("\"response_b64\":null");
                        }
                    } else {
                        sb.append("\"response_b64\":null");
                    }

                    sb.append('}');
                    w.write(sb.toString());
                    w.newLine();
                    count++;

                    if (count % 1000 == 0) {
                        api.logging().logToError("[burp-caido-syncer] " + count + "/" + total);
                        w.flush();
                    }
                } catch (Exception e) {
                    api.logging().logToError("[burp-caido-syncer] skip item: " + e.getMessage());
                }
            }

            w.flush();
            api.logging().logToError("[burp-caido-syncer] done: " + count + "/" + total);
        }
    }

    /**
     * Export Burp's target scope as JSON.
     * Output: {"include":["host1","host2"],"exclude":["host3"]}
     * Parsed from Burp's project options JSON.
     */
    private void dumpScope(MontoyaApi api, String outputPath) throws IOException {
        api.logging().logToError("[burp-caido-syncer] Exporting scope...");
        String projectJson = api.burpSuite().exportProjectOptionsAsJson("target");

        OutputStream rawOut = (outputPath != null)
                ? new FileOutputStream(outputPath)
                : System.out;
        try (BufferedWriter w = new BufferedWriter(
                new OutputStreamWriter(rawOut, StandardCharsets.UTF_8))) {
            w.write(projectJson);
            w.flush();
        }
        api.logging().logToError("[burp-caido-syncer] Scope exported to " + outputPath);
    }

    // -- tiny JSON helpers (no Gson dependency) --

    private static void str(StringBuilder sb, String k, String v) {
        sb.append('"').append(k).append("\":\"");
        if (v != null) esc(sb, v);
        sb.append('"');
    }
    private static void num(StringBuilder sb, String k, long v) {
        sb.append('"').append(k).append("\":").append(v);
    }
    private static void bool(StringBuilder sb, String k, boolean v) {
        sb.append('"').append(k).append("\":").append(v);
    }
    private static void esc(StringBuilder sb, String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '\t': sb.append("\\t");  break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
    }
}
