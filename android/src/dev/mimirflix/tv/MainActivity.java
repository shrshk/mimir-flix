package dev.mimirflix.tv;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.Log;
import android.view.KeyEvent;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * mimir-tv on the TV: a full-screen WebView on mimir-tv's TV mode (?tv=1), which does its own remote
 * navigation. BACK asks the page first (window.tvBack) and closes the app when there is nothing to go back to.
 * Playing starts in the TV's Jellyfin app (mimir-tv tells Jellyfin to play there).
 */
public class MainActivity extends Activity {
    private WebView web;
    private String base;
    private boolean failed;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        base = Server.url(this);
        if (base.isEmpty()) {  // first launch: ask where mimir-tv is
            startActivity(new Intent(this, ServerActivity.class));
            finish();
            return;
        }
        web = new WebView(this);
        web.setBackgroundColor(0xFF000000);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                // Only mimir-tv opens here; nothing navigates away to the web. Same scheme, host and port: a
                // prefix match would also pass http://host:87230 or https://lan.example.com.evil.
                android.net.Uri u = request.getUrl(), home = android.net.Uri.parse(base);
                return !(eq(u.getScheme(), home.getScheme()) && eq(u.getHost(), home.getHost())
                        && u.getPort() == home.getPort());
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (!request.isForMainFrame()) return;
                Log.w("MimirFlixTV", "page failed: " + error.getErrorCode() + " " + error.getDescription());
                showError(String.valueOf(error.getDescription()));
            }
        });
        setContentView(web);
        web.requestFocus();
        web.loadUrl(base + "/?tv=1");
    }

    private static boolean eq(String a, String b) {
        return a != null && a.equalsIgnoreCase(b);
    }

    private void showError(String why) {
        String html = "<body style='background:#000;color:#fff;font-family:sans-serif;padding:8vw'>"
                + "<h2 style='color:#e50914'>mimir-tv is not answering</h2>"
                + "<p style='color:#aaa'>" + TextUtils.htmlEncode(base) + "<br>" + TextUtils.htmlEncode(why) + "</p>"
                + "<p style='color:#777'>Is the Mac that runs it on? Press OK to change the address, BACK to close.</p></body>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        failed = true;
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        // On the error page, OK opens the Server screen (a wrong or changed address is fixed from the TV).
        if (failed && e.getAction() == KeyEvent.ACTION_UP
                && (e.getKeyCode() == KeyEvent.KEYCODE_DPAD_CENTER || e.getKeyCode() == KeyEvent.KEYCODE_ENTER)) {
            startActivity(new Intent(this, ServerActivity.class));
            return true;
        }
        return super.dispatchKeyEvent(e);
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.tvBack ? window.tvBack() : false", handled -> {
            if (!"true".equals(handled)) finish();
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onPause() {
        if (web != null) web.onPause();
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (web != null) web.destroy();
        super.onDestroy();
    }
}
