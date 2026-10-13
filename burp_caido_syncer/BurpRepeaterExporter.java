package burp;

import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.core.ByteArray;
import burp.api.montoya.core.ToolSource;
import burp.api.montoya.core.ToolType;
import burp.api.montoya.http.HttpService;
import burp.api.montoya.http.message.HttpRequestResponse;
import burp.api.montoya.http.message.requests.HttpRequest;
import burp.api.montoya.http.message.responses.HttpResponse;
import burp.api.montoya.ui.Selection;
import burp.api.montoya.ui.editor.extension.EditorCreationContext;
import burp.api.montoya.ui.editor.extension.ExtensionProvidedHttpRequestEditor;
import burp.api.montoya.ui.editor.extension.HttpRequestEditorProvider;

import javax.swing.AbstractButton;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTabbedPane;
import javax.swing.SwingUtilities;

import java.awt.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.List;
import java.lang.reflect.InvocationTargetException;
import java.util.concurrent.atomic.AtomicReference;

/**
 * One-shot Repeater tab exporter.
 *
 * Triggered by CLI arg "exportRepeater=/path/to/out.ndjson".
 * Uses editor-provider hooks + Swing tab walk: selecting each Repeater tab
 * triggers Burp to bind its data to our invisible editor, which captures it.
 */
public class BurpRepeaterExporter implements BurpExtension {

    private static volatile String currentWalkTabName;
    private static final List<CapturedTab> CAPTURED = Collections.synchronizedList(new ArrayList<>());
    private static final Set<String> SEEN_TAB_NAMES = Collections.synchronizedSet(new HashSet<>());

    @Override
    public void initialize(MontoyaApi api) {
        api.extension().setName("BurpRepeaterExporter");

        String[] args = api.burpSuite().commandLineArguments().toArray(new String[0]);
        String outputPath = null;
        boolean triggered = false;
        for (String arg : args) {
            if (arg.startsWith("exportRepeater=")) {
                triggered = true;
                String val = arg.substring("exportRepeater=".length()).trim();
                if (!val.isEmpty()) outputPath = val;
            } else if (arg.equals("exportRepeater")) {
                triggered = true;
            }
        }

        if (!triggered) {
            api.logging().logToOutput(
                "{\"status\":\"loaded\",\"message\":\"Pass exportRepeater=/path CLI arg to trigger export.\"}");
            return;
        }

        api.userInterface().registerHttpRequestEditorProvider(new CaptureEditorProvider());

        final String outPath = outputPath;
        Thread worker = new Thread(() -> {
            try {
                System.err.println("[repeater-export] Waiting for Burp UI to load...");
                if (!waitForBurpUI(15)) {
                    System.err.println("[repeater-export] Burp UI not found after 15s, trying anyway...");
                }
                Thread.sleep(2000);
                walkAndExport(api, outPath);
            } catch (Exception ex) {
                System.err.println("[repeater-export] Failed: " + ex.getMessage());
                ex.printStackTrace(System.err);
            } finally {
                try { api.extension().unload(); } catch (Exception ignored) {}
                try { api.burpSuite().shutdown(); } catch (Exception ignored) {}
            }
        }, "b2c-repeater-export");
        worker.setDaemon(false);
        worker.start();
    }

    private void walkAndExport(MontoyaApi api, String outputPath) throws Exception {
        System.err.println("[repeater-export] Starting Repeater tab walk...");
        performTabWalk(api);

        Thread.sleep(500);

        System.err.println("[repeater-export] Captured " + CAPTURED.size() + " Repeater tabs");
        writeNdjson(api, outputPath);
    }

    private boolean waitForBurpUI(int maxSeconds) {
        for (int i = 0; i < maxSeconds; i++) {
            for (Frame f : Frame.getFrames()) {
                if (f.isVisible() && f.getTitle() != null
                        && f.getTitle().toLowerCase().contains("burp")) {
                    System.err.println("[repeater-export] Burp UI detected");
                    return true;
                }
            }
            try { Thread.sleep(1000); } catch (InterruptedException e) { return false; }
        }
        return false;
    }

    private void performTabWalk(MontoyaApi api) throws Exception {
        // Find panes on EDT
        AtomicReference<JTabbedPane> toolTabsRef = new AtomicReference<>();
        AtomicReference<JTabbedPane> repeaterPaneRef = new AtomicReference<>();
        AtomicReference<int[]> indicesRef = new AtomicReference<>();

        SwingUtilities.invokeAndWait(() -> {
            for (Frame frame : Frame.getFrames()) {
                ToolTabLocation loc = findToolTab(frame, "Repeater");
                if (loc != null) {
                    toolTabsRef.set(loc.pane);
                    indicesRef.set(new int[]{loc.index, loc.pane.getSelectedIndex()});

                    if (loc.pane.getSelectedIndex() != loc.index)
                        loc.pane.setSelectedIndex(loc.index);

                    Component repeaterRoot = loc.pane.getComponentAt(loc.index);
                    JTabbedPane rp = findRepeaterTabPane(repeaterRoot);
                    repeaterPaneRef.set(rp);
                    break;
                }
            }
        });

        JTabbedPane toolTabs = toolTabsRef.get();
        JTabbedPane repeaterPane = repeaterPaneRef.get();
        int[] indices = indicesRef.get();

        if (toolTabs == null) {
            System.err.println("[repeater-export] Could not find Repeater tool tab");
            return;
        }
        if (repeaterPane == null) {
            System.err.println("[repeater-export] Could not find Repeater tab pane");
            return;
        }

        int total = repeaterPane.getTabCount();
        System.err.println("[repeater-export] Found " + total + " Repeater tabs");

        // Cycle tabs from background thread, selecting each on EDT
        for (int i = 0; i < total; i++) {
            final int idx = i;
            SwingUtilities.invokeAndWait(() -> {
                String title = getTabTitle(repeaterPane, idx);
                currentWalkTabName = title != null ? title : ("Tab " + (idx + 1));
                repeaterPane.setSelectedIndex(idx);
            });

            // Give EDT time to process callbacks and bind data
            Thread.sleep(500);

            if ((i + 1) % 25 == 0 || i + 1 == total) {
                System.err.println("[repeater-export] Progress: " + (i + 1) + "/" + total
                    + " walked, " + CAPTURED.size() + " captured");
            }
        }

        // Restore original tool tab
        SwingUtilities.invokeLater(() -> {
            if (indices[1] >= 0 && indices[1] < toolTabs.getTabCount())
                toolTabs.setSelectedIndex(indices[1]);
        });
    }

    private JTabbedPane findRepeaterTabPane(Component root) {
        if (root instanceof JTabbedPane pane && isRepeaterTabPane(pane)) {
            return pane;
        }
        if (root instanceof Container container) {
            for (Component child : container.getComponents()) {
                JTabbedPane found = findRepeaterTabPane(child);
                if (found != null) return found;
            }
        }
        return null;
    }

    private boolean isRepeaterTabPane(JTabbedPane pane) {
        if (pane == null || pane.getTabCount() <= 1) return false;
        Set<String> viewTabs = Set.of(
            "beautify", "custom actions", "headers", "hex",
            "inspector", "params", "pretty", "raw", "render");
        int titled = 0, known = 0;
        for (int i = 0; i < pane.getTabCount(); i++) {
            String t = pane.getTitleAt(i);
            if (t == null || t.isBlank()) continue;
            titled++;
            if (viewTabs.contains(t.trim().toLowerCase())) known++;
        }
        return titled == 0 || titled != known;
    }

    private String getTabTitle(JTabbedPane pane, int i) {
        if (pane == null || i < 0 || i >= pane.getTabCount()) return null;
        String t = pane.getTitleAt(i);
        if (t != null && !t.isBlank()) return t.trim();
        t = pane.getToolTipTextAt(i);
        if (t != null && !t.isBlank()) return t.trim();
        return extractText(pane.getTabComponentAt(i));
    }

    private String extractText(Component c) {
        if (c == null) return null;
        if (c instanceof JLabel l && l.getText() != null && !l.getText().isBlank()) return l.getText().trim();
        if (c instanceof AbstractButton b && b.getText() != null && !b.getText().isBlank()) return b.getText().trim();
        if (c instanceof Container ct) {
            for (Component ch : ct.getComponents()) {
                String r = extractText(ch);
                if (r != null) return r;
            }
        }
        return null;
    }

    private ToolTabLocation findToolTab(Component root, String title) {
        if (root instanceof JTabbedPane pane) {
            for (int i = 0; i < pane.getTabCount(); i++)
                if (title.equalsIgnoreCase(pane.getTitleAt(i)))
                    return new ToolTabLocation(pane, i);
        }
        if (root instanceof Container c) {
            for (Component ch : c.getComponents()) {
                ToolTabLocation loc = findToolTab(ch, title);
                if (loc != null) return loc;
            }
        }
        return null;
    }

    // -- Editor provider: silently captures data when Burp binds a Repeater tab --

    private static class CaptureEditorProvider implements HttpRequestEditorProvider {
        @Override
        public ExtensionProvidedHttpRequestEditor provideHttpRequestEditor(EditorCreationContext ctx) {
            return new CaptureEditor(ctx);
        }
    }

    private static class CaptureEditor implements ExtensionProvidedHttpRequestEditor {
        private final EditorCreationContext ctx;
        private final JPanel hidden = new JPanel();
        private HttpRequestResponse current;

        CaptureEditor(EditorCreationContext ctx) { this.ctx = ctx; }

        @Override public String caption() { return "B2C"; }
        @Override public Component uiComponent() { return hidden; }
        @Override public Selection selectedData() { return null; }
        @Override public boolean isModified() { return false; }
        @Override public HttpRequest getRequest() { return current == null ? null : current.request(); }

        @Override
        public void setRequestResponse(HttpRequestResponse rr) {
            this.current = rr;
            capture(ctx, rr);
        }

        @Override
        public boolean isEnabledFor(HttpRequestResponse rr) {
            capture(ctx, rr);
            return false;
        }
    }

    private static void capture(EditorCreationContext ctx, HttpRequestResponse rr) {
        if (rr == null || ctx == null) return;
        ToolSource ts = ctx.toolSource();
        if (ts == null || ts.toolType() != ToolType.REPEATER) return;

        HttpRequest req = rr.request();
        if (req == null) return;

        byte[] reqBytes = null;
        byte[] respBytes = null;
        HttpService svc = null;

        try { ByteArray ba = req.toByteArray(); if (ba != null) reqBytes = ba.getBytes(); } catch (Exception ignored) {}
        if (reqBytes == null || reqBytes.length == 0) return;

        HttpResponse resp = rr.response();
        if (resp != null) {
            try { ByteArray ba = resp.toByteArray(); if (ba != null) respBytes = ba.getBytes(); } catch (Exception ignored) {}
        }
        try { svc = req.httpService(); } catch (Exception ignored) {}

        String tabName = currentWalkTabName;
        if (tabName == null) tabName = "Untitled";
        if (!SEEN_TAB_NAMES.add(tabName)) return;

        CAPTURED.add(new CapturedTab(tabName,
            svc, reqBytes, respBytes));
    }

    // -- Write NDJSON --

    private void writeNdjson(MontoyaApi api, String outputPath) throws IOException {
        OutputStream out = (outputPath != null) ? new FileOutputStream(outputPath) : System.out;
        try (BufferedWriter w = new BufferedWriter(new OutputStreamWriter(out, StandardCharsets.UTF_8), 1 << 16)) {
            Base64.Encoder b64 = Base64.getEncoder();
            int count = 0;
            for (CapturedTab tab : CAPTURED) {
                StringBuilder sb = new StringBuilder(4096);
                sb.append('{');
                str(sb, "tab_name", tab.tabName); sb.append(',');
                if (tab.service != null) {
                    str(sb, "host", tab.service.host()); sb.append(',');
                    num(sb, "port", tab.service.port()); sb.append(',');
                    bool(sb, "is_tls", tab.service.secure());
                } else {
                    sb.append("\"host\":\"\",\"port\":443,\"is_tls\":true");
                }
                sb.append(',');
                if (tab.reqBytes != null) {
                    sb.append("\"request_b64\":\"").append(b64.encodeToString(tab.reqBytes)).append('"');
                } else { sb.append("\"request_b64\":null"); }
                sb.append(',');
                if (tab.respBytes != null) {
                    sb.append("\"response_b64\":\"").append(b64.encodeToString(tab.respBytes)).append('"');
                } else { sb.append("\"response_b64\":null"); }
                sb.append('}');
                w.write(sb.toString());
                w.newLine();
                count++;
            }
            w.flush();
            api.logging().logToError("[repeater-export] Wrote " + count + " tabs to NDJSON");
        }
    }

    // -- Helpers --

    private record CapturedTab(String tabName, HttpService service, byte[] reqBytes, byte[] respBytes) {}
    private record ToolTabLocation(JTabbedPane pane, int index) {}

    private static void str(StringBuilder sb, String k, String v) {
        sb.append('"').append(k).append("\":\""); if (v != null) esc(sb, v); sb.append('"');
    }
    private static void num(StringBuilder sb, String k, long v) { sb.append('"').append(k).append("\":").append(v); }
    private static void bool(StringBuilder sb, String k, boolean v) { sb.append('"').append(k).append("\":").append(v); }
    private static void esc(StringBuilder sb, String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break; case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break; case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default: if (c < 0x20) sb.append(String.format("\\u%04x", (int)c)); else sb.append(c);
            }
        }
    }
}
