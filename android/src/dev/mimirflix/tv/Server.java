package dev.mimirflix.tv;

import android.content.Context;

/**
 * Where mimir-tv is: the address typed on the Server screen, kept on the TV. A build can carry a starting address
 * (MIMIR_TV_URL in build.sh) so a TV set up by its owner needs no typing; one typed on the TV wins over it.
 */
final class Server {
    private static final String PREFS = "server", KEY = "url";

    private Server() {
    }

    static String url(Context c) {
        String typed = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "");
        String built = c.getString(R.string.mimir_tv_url);
        return (typed.isEmpty() ? built : typed).replaceAll("/+$", "");
    }

    static void save(Context c, String url) {
        c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, url).apply();
    }

    /** What the person typed, as an address: "192.168.1.20:8723" becomes "http://192.168.1.20:8723". */
    static String normalize(String typed) {
        String s = typed.trim().replaceAll("/+$", "");
        if (s.isEmpty()) return "";
        return s.matches("(?i)^https?://.*") ? s : "http://" + s;
    }
}
