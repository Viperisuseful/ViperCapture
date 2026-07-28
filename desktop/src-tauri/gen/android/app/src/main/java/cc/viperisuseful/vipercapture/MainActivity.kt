package cc.viperisuseful.vipercapture

import android.os.Bundle
import androidx.activity.enableEdgeToEdge
import androidx.activity.OnBackPressedCallback

class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
    onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
      override fun handleOnBackPressed() {
        moveTaskToBack(true)
      }
    })
  }
}
