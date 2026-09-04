package com.adjacentgarden.meeting;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.ImageView;

public final class MeetingSlidesActivity extends Activity {
    private static final long SLIDE_INTERVAL_MS = 3000L;
    private static final String ACTION_PAUSE = "com.adjacentgarden.meeting.PAUSE";
    private static final String ACTION_RESUME = "com.adjacentgarden.meeting.RESUME";
    private static final int LEGACY_IMMERSIVE_FLAGS =
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private ImageView imageView;
    private Bitmap slideOne;
    private Bitmap slideTwo;
    private boolean showingFirst = true;
    private boolean paused = false;
    private final Runnable advanceSlide = new Runnable() {
        @Override
        public void run() {
            showingFirst = !showingFirst;
            imageView.setImageBitmap(showingFirst ? slideOne : slideTwo);
            handler.postDelayed(this, SLIDE_INTERVAL_MS);
        }
    };
    private final BroadcastReceiver controlReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            if (ACTION_PAUSE.equals(action)) {
                paused = true;
                handler.removeCallbacks(advanceSlide);
            } else if (ACTION_RESUME.equals(action) && paused) {
                paused = false;
                handler.removeCallbacks(advanceSlide);
                handler.postDelayed(advanceSlide, SLIDE_INTERVAL_MS);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(
                WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);

        // Decode both 1920x1080 slides before the first frame. Switching then
        // only swaps an in-memory bitmap: no Gallery restart and no UI flash.
        slideOne = BitmapFactory.decodeResource(getResources(), R.drawable.meeting_slide_1);
        slideTwo = BitmapFactory.decodeResource(getResources(), R.drawable.meeting_slide_2);
        if (slideOne == null || slideTwo == null) {
            finishAndRemoveTask();
            return;
        }

        imageView = new ImageView(this);
        imageView.setBackgroundColor(Color.BLACK);
        imageView.setScaleType(ImageView.ScaleType.FIT_XY);
        imageView.setImageBitmap(slideOne);
        setContentView(imageView);
        IntentFilter controls = new IntentFilter();
        controls.addAction(ACTION_PAUSE);
        controls.addAction(ACTION_RESUME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(controlReceiver, controls, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(controlReceiver, controls);
        }
        enterImmersiveMode();
        handler.postDelayed(advanceSlide, SLIDE_INTERVAL_MS);
    }

    @Override
    protected void onResume() {
        super.onResume();
        enterImmersiveMode();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            enterImmersiveMode();
        }
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        unregisterReceiver(controlReceiver);
        if (slideOne != null) slideOne.recycle();
        if (slideTwo != null) slideTwo.recycle();
        super.onDestroy();
    }

    private void enterImmersiveMode() {
        getWindow().getDecorView().setSystemUiVisibility(LEGACY_IMMERSIVE_FLAGS);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.hide(WindowInsets.Type.systemBars());
                controller.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        }
    }
}
