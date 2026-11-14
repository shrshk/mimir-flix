package dev.mimirflix.tv;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RectF;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Messages from the phone (I4), drawn over whatever is on: a live doodle or a photo. mimir-tv starts
 * this over ADB when a message comes; it reads mimir-tv's event stream, draws what arrives, fades a few seconds after
 * the last message and stops itself, so it costs the TV nothing at rest. The window takes no focus and no input, so
 * playback underneath carries on. Nothing is saved: the photo is decoded in memory and dropped when it fades.
 */
public class OverlayService extends Service {
    private static final String TAG = "MimirFlixOverlay";
    private static final long FADE_AFTER_MS = 6000, PHOTO_FOR_MS = 9000, STOP_AFTER_MS = 15000;
    /** "Until cleared" on the phone (hold 0) keeps a drawing an hour at most; Clear takes it off at once. */
    private static final long UNTIL_CLEARED_MS = 3600_000, AFTER_CLEAR_MS = 300;

    private final Handler main = new Handler(Looper.getMainLooper());
    private String base;
    private DrawView view;
    private volatile boolean running;
    private Thread reader;

    @Override
    public void onCreate() {
        super.onCreate();
        base = Server.url(this);
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel("overlay", "Messages", NotificationManager.IMPORTANCE_MIN));
        startForeground(1, new Notification.Builder(this, "overlay").setContentTitle("mimir-tv")
                .setSmallIcon(android.R.drawable.ic_menu_edit).build());
        view = new DrawView(this);
        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        try {
            ((WindowManager) getSystemService(WINDOW_SERVICE)).addView(view, lp);
        } catch (RuntimeException e) {  // no permission to draw over other apps (appops SYSTEM_ALERT_WINDOW)
            Log.w(TAG, "cannot draw over other apps: " + e);
            stopSelf();
            return;
        }
        running = true;
        reader = new Thread(this::read, "overlay-events");
        reader.start();
        main.postDelayed(stop, STOP_AFTER_MS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_NOT_STICKY;  // started again by mimir-tv when the next message comes
    }

    private final Runnable stop = this::stopSelf;
    private final Runnable fade = () -> view.fadeOut();

    /** The event stream, read until the service stops; a dropped connection is tried again a second later. */
    private void read() {
        while (running) {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(base + "/api/tv/events").openConnection();
                c.setReadTimeout(30000);
                c.setRequestProperty("Accept", "text/event-stream");
                BufferedReader in = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
                String line;
                while (running && (line = in.readLine()) != null) {
                    if (!line.startsWith("data: ")) continue;
                    JSONObject e = new JSONObject(line.substring(6));
                    if ("photo".equals(e.optString("kind"))) {
                        Bitmap b = fetch(base + e.getString("url"));
                        main.post(() -> show(e, b));
                    } else {
                        main.post(() -> show(e, null));
                    }
                }
            } catch (Exception e) {
                if (running) Log.w(TAG, "events: " + e.getClass().getSimpleName());
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException ignored) {
                    return;
                }
            } finally {
                if (c != null) c.disconnect();
            }
        }
    }

    /** A photo, scaled while decoding so a large one does not take the TV's memory. */
    private Bitmap fetch(String url) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        try (InputStream in = c.getInputStream()) {
            byte[] data = readAll(in);
            BitmapFactory.Options o = new BitmapFactory.Options();
            o.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(data, 0, data.length, o);
            o.inSampleSize = 1;
            while (Math.max(o.outWidth, o.outHeight) / o.inSampleSize > 1920) o.inSampleSize *= 2;
            o.inJustDecodeBounds = false;
            return BitmapFactory.decodeByteArray(data, 0, data.length, o);
        } finally {
            c.disconnect();
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
        return out.toByteArray();
    }

    private void show(JSONObject e, Bitmap photo) {
        String kind = e.optString("kind");
        long hold = FADE_AFTER_MS;
        if ("stroke".equals(kind)) {
            view.addStroke(e);
            hold = holdMs(e);
        } else if ("clear".equals(kind)) {
            view.clear();
            hold = AFTER_CLEAR_MS;
        }
        else if ("photo".equals(kind) && photo != null) {
            view.photo(photo);
            hold = PHOTO_FOR_MS;
        }
        main.removeCallbacks(fade);
        main.removeCallbacks(stop);
        main.postDelayed(fade, hold);
        main.postDelayed(stop, hold + STOP_AFTER_MS);
    }

    /** How long the phone asked the TV to keep the drawing after its last stroke (an older phone page: the default). */
    static long holdMs(JSONObject e) {
        int s = e.optInt("hold", -1);
        return s < 0 ? FADE_AFTER_MS : s == 0 ? UNTIL_CLEARED_MS : Math.min(s * 1000L, UNTIL_CLEARED_MS);
    }

    @Override
    public void onDestroy() {
        running = false;
        main.removeCallbacksAndMessages(null);
        if (reader != null) reader.interrupt();
        if (view != null && view.isAttachedToWindow()) {
            ((WindowManager) getSystemService(WINDOW_SERVICE)).removeView(view);
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /** The drawing: strokes as smooth curves through their points' midpoints, widths by pressure (the phone's brush). */
    static class DrawView extends View {
        private final Map<Integer, Stroke> strokes = new LinkedHashMap<>();
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private Bitmap photo;

        DrawView(Context c) {
            super(c);
            setWillNotDraw(false);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeJoin(Paint.Join.ROUND);
            setAlpha(0f);
        }

        private void appear() {
            animate().cancel();
            animate().alpha(1f).setDuration(200).start();
        }

        void fadeOut() {
            animate().alpha(0f).setDuration(600).withEndAction(() -> {
                strokes.clear();
                if (photo != null) photo.recycle();
                photo = null;
                invalidate();
            }).start();
        }

        void clear() {
            strokes.clear();
            invalidate();
        }

        void addStroke(JSONObject e) {
            int id = e.optInt("id");
            Stroke s = strokes.get(id);
            if (s == null) {
                s = new Stroke(Color.parseColor(e.optString("color", "#ffffff")), (float) e.optDouble("width", 0.01),
                        (float) e.optDouble("aspect", 16.0 / 9));
                strokes.put(id, s);
            }
            JSONArray pts = e.optJSONArray("points");
            for (int i = 0; pts != null && i < pts.length(); i++) {
                JSONArray p = pts.optJSONArray(i);
                if (p != null) s.points.add(new float[]{(float) p.optDouble(0), (float) p.optDouble(1), (float) p.optDouble(2)});
            }
            appear();
            invalidate();
        }

        void photo(Bitmap b) {
            if (photo != null) photo.recycle();
            photo = b;
            appear();
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            int w = getWidth(), h = getHeight();
            if (photo != null) {
                canvas.drawColor(0xE6000000);
                float k = Math.min(w * 0.9f / photo.getWidth(), h * 0.9f / photo.getHeight());
                float pw = photo.getWidth() * k, ph = photo.getHeight() * k;
                canvas.drawBitmap(photo, null, new RectF((w - pw) / 2, (h - ph) / 2, (w + pw) / 2, (h + ph) / 2), null);
            }
            // The phone's box keeps its shape: as large as fits, centred (a 16:9 box fills the screen; a tall one, the
            // phone held upright, stands in the middle). x and y are fractions of the box, widths of its width.
            for (Stroke s : strokes.values()) {
                paint.setColor(s.color);
                float bw = Math.min(w, h * s.aspect), bh = bw / s.aspect, ox = (w - bw) / 2, oy = (h - bh) / 2;
                List<float[]> p = s.points;
                for (int i = 1; i < p.size(); i++) {
                    float[] a = p.get(i - 1), b = p.get(i), c = i + 1 < p.size() ? p.get(i + 1) : b;
                    paint.setStrokeWidth(s.width * bw * (0.5f + b[2]));
                    android.graphics.Path path = new android.graphics.Path();
                    path.moveTo(ox + (a[0] + b[0]) / 2 * bw, oy + (a[1] + b[1]) / 2 * bh);
                    path.quadTo(ox + b[0] * bw, oy + b[1] * bh, ox + (b[0] + c[0]) / 2 * bw, oy + (b[1] + c[1]) / 2 * bh);
                    canvas.drawPath(path, paint);
                }
                if (p.size() == 1) {  // a dot
                    paint.setStrokeWidth(s.width * bw);
                    canvas.drawPoint(ox + p.get(0)[0] * bw, oy + p.get(0)[1] * bh, paint);
                }
            }
        }
    }

    static class Stroke {
        final int color;
        final float width, aspect;
        final List<float[]> points = new ArrayList<>();

        Stroke(int color, float width, float aspect) {
            this.color = color;
            this.width = width;
            this.aspect = aspect > 0 ? aspect : 16f / 9;
        }
    }
}
