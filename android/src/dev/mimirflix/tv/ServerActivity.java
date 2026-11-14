package dev.mimirflix.tv;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * The Server screen: the address of mimir-tv, typed with the remote's keyboard the first time the app opens (or
 * when mimir-tv is not answering). It is checked (mimir-tv answers /api/version) before it is kept.
 */
public class ServerActivity extends Activity {
    private EditText field;
    private TextView status;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        int pad = (int) (getResources().getDisplayMetrics().widthPixels * 0.08);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(pad, pad / 2, pad, pad / 2);
        box.setBackgroundColor(0xFF0D1526);

        TextView title = new TextView(this);
        title.setText("Where is mimir-tv?");
        title.setTextColor(Color.WHITE);
        title.setTextSize(30);
        TextView help = new TextView(this);
        help.setText("Type the address the Mac running mimir-tv shows on its Settings page (TV app card), "
                + "for example 192.168.1.20:8723 or https://lan.example.com");
        help.setTextColor(0xFFA3A3A3);
        help.setTextSize(18);
        help.setPadding(0, 16, 0, 24);

        field = new EditText(this);
        field.setSingleLine(true);
        field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        field.setImeOptions(EditorInfo.IME_ACTION_DONE);
        field.setTextColor(Color.WHITE);
        field.setTextSize(24);
        field.setHint("192.168.1.20:8723");
        field.setHintTextColor(0xFF555555);
        field.setText(Server.url(this));
        field.setOnEditorActionListener((v, action, e) -> {
            check();
            return true;
        });

        Button save = new Button(this);
        save.setText("Connect");
        save.setTextSize(20);
        save.setOnClickListener(v -> check());
        status = new TextView(this);
        status.setTextSize(18);
        status.setPadding(0, 20, 0, 0);

        box.addView(title);
        box.addView(help);
        box.addView(field);
        box.addView(save);
        box.addView(status);
        setContentView(box);
        field.requestFocus();
    }

    private void check() {
        String url = Server.normalize(field.getText().toString());
        if (url.isEmpty()) return;
        status.setTextColor(0xFFA3A3A3);
        status.setText("Checking " + url + " …");
        Handler ui = new Handler(Looper.getMainLooper());
        new Thread(() -> {
            String problem = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url + "/api/version").openConnection();
                c.setConnectTimeout(6000);
                c.setReadTimeout(6000);
                if (c.getResponseCode() != 200) problem = "it answered " + c.getResponseCode();
                c.disconnect();
            } catch (IOException e) {  // no answer, a bad name or address: told to the person, who fixes it
                problem = "no answer (" + e.getClass().getSimpleName() + ")";
            }
            String why = problem;
            ui.post(() -> {
                if (why != null) {
                    status.setTextColor(0xFFE50914);
                    status.setText("mimir-tv is not at " + url + ": " + why);
                    return;
                }
                Server.save(this, url);
                startActivity(new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP));
                finish();
            });
        }).start();
    }
}
