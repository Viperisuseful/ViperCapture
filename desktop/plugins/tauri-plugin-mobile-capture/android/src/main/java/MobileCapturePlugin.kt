package cc.viperisuseful.vipercapture.mobilecapture

import android.app.Activity
import android.app.DownloadManager
import android.content.ContentValues
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.net.Uri
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Base64
import android.view.View
import android.view.ViewGroup
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import app.tauri.annotation.Command
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin
import java.io.ByteArrayOutputStream
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max
import kotlin.math.roundToInt

@TauriPlugin
class MobileCapturePlugin(private val activity: Activity) : Plugin(activity) {
    private val captures = LinkedHashMap<String, StoredCapture>()
    private val mainHandler = Handler(Looper.getMainLooper())

    data class StoredCapture(
        val bytes: ByteArray,
        val name: String,
        val mimeType: String
    )

    companion object {
        private const val MAX_PIXELS = 16_000_000L
        private const val MAX_HISTORY = 6

        init {
            WebView.enableSlowWholeDocumentDraw()
        }
    }

    @Command
    fun capture(invoke: Invoke) {
        val args = invoke.getArgs()
        val rawUrl = args.optString("url", "")
        val parsed = runCatching { Uri.parse(rawUrl) }.getOrNull()
        if (parsed == null || parsed.scheme !in listOf("http", "https") || parsed.host.isNullOrBlank()) {
            invoke.reject("Enter a valid public HTTP or HTTPS URL")
            return
        }

        val width = args.optInt("width", 1280).coerceIn(240, 3840)
        val height = args.optInt("height", 720).coerceIn(240, 4320)
        val density = args.optDouble("density", 1.0).coerceIn(0.5, 3.0)
        val fullPage = args.optBoolean("fullPage", true)
        val output = args.optString("output", "png").lowercase(Locale.US)
        val quality = args.optInt("quality", 90).coerceIn(1, 100)
        val transparent = args.optBoolean("transparent", false)
        val lazyLoad = args.optString("lazyLoad", "thorough")
        val waitDelay = args.optInt("waitDelayMs", 1000).coerceIn(0, 15_000)
        val timeout = args.optInt("timeoutMs", 15_000).coerceIn(3_000, 30_000)

        if (output !in listOf("png", "jpeg", "webp")) {
            invoke.reject("Unsupported image format")
            return
        }
        if ((width * density).roundToInt().toLong() * (height * density).roundToInt() > MAX_PIXELS) {
            invoke.reject("The requested viewport exceeds Android's 16 megapixel safety limit")
            return
        }

        activity.runOnUiThread {
            render(
                invoke,
                parsed,
                width,
                height,
                density,
                fullPage,
                output,
                quality,
                transparent,
                lazyLoad,
                waitDelay,
                timeout
            )
        }
    }

    private fun render(
        invoke: Invoke,
        url: Uri,
        width: Int,
        height: Int,
        outputDensity: Double,
        fullPage: Boolean,
        output: String,
        quality: Int,
        transparent: Boolean,
        lazyLoad: String,
        waitDelay: Int,
        timeout: Int
    ) {
        val completed = AtomicBoolean(false)
        val root = activity.findViewById<ViewGroup>(android.R.id.content)
        val systemDensity = activity.resources.displayMetrics.density
        val viewWidth = max(1, (width * systemDensity).roundToInt())
        val viewHeight = max(1, (height * systemDensity).roundToInt())
        val webView = WebView(activity)

        fun cleanup() {
            root.removeView(webView)
            webView.stopLoading()
            webView.destroy()
        }

        fun fail(message: String) {
            if (completed.compareAndSet(false, true)) {
                cleanup()
                invoke.reject(message)
            }
        }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            useWideViewPort = false
            loadWithOverviewMode = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mediaPlaybackRequiresUserGesture = true
        }
        webView.setLayerType(View.LAYER_TYPE_SOFTWARE, null)
        webView.setBackgroundColor(if (transparent && output != "jpeg") Color.TRANSPARENT else Color.WHITE)
        webView.translationX = -viewWidth * 2f
        root.addView(webView, ViewGroup.LayoutParams(viewWidth, viewHeight))

        val timeoutTask = Runnable { fail("The page did not finish rendering before the timeout") }
        mainHandler.postDelayed(timeoutTask, timeout.toLong())

        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, loadedUrl: String) {
                if (completed.get()) return
                val lazyScript = if (lazyLoad == "none") {
                    "window.scrollTo(0,0); true;"
                } else {
                    """
                    (async () => {
                      const step = Math.max(320, Math.floor(window.innerHeight * 0.8));
                      for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(resolve => setTimeout(resolve, ${if (lazyLoad == "thorough") 90 else 35}));
                      }
                      window.scrollTo(0, 0);
                      return true;
                    })()
                    """.trimIndent()
                }
                view.evaluateJavascript(lazyScript) {
                    mainHandler.postDelayed({
                        if (!completed.get()) {
                            drawCapture(
                                invoke,
                                root,
                                webView,
                                completed,
                                width,
                                height,
                                outputDensity,
                                fullPage,
                                output,
                                quality,
                                transparent
                            )
                        }
                    }, waitDelay.toLong())
                }
            }

            override fun onReceivedError(
                view: WebView,
                request: android.webkit.WebResourceRequest,
                error: android.webkit.WebResourceError
            ) {
                if (request.isForMainFrame) fail("Could not load the page: ${error.description}")
            }
        }

        webView.loadUrl(url.toString())
    }

    private fun drawCapture(
        invoke: Invoke,
        root: ViewGroup,
        webView: WebView,
        completed: AtomicBoolean,
        width: Int,
        height: Int,
        outputDensity: Double,
        fullPage: Boolean,
        output: String,
        quality: Int,
        transparent: Boolean
    ) {
        val contentCssHeight = max(height, webView.contentHeight)
        val outputWidth = max(1, (width * outputDensity).roundToInt())
        val requestedHeight = if (fullPage) contentCssHeight else height
        var outputHeight = max(1, (requestedHeight * outputDensity).roundToInt())
        if (outputWidth.toLong() * outputHeight > MAX_PIXELS) {
            outputHeight = (MAX_PIXELS / outputWidth).toInt().coerceAtLeast(1)
        }

        val systemDensity = activity.resources.displayMetrics.density
        val layoutHeight = max(1, (requestedHeight * systemDensity).roundToInt())
        webView.measure(
            ViewGroup.LayoutParams.MATCH_PARENT.let {
                android.view.View.MeasureSpec.makeMeasureSpec(webView.width, android.view.View.MeasureSpec.EXACTLY)
            },
            android.view.View.MeasureSpec.makeMeasureSpec(layoutHeight, android.view.View.MeasureSpec.EXACTLY)
        )
        webView.layout(0, 0, webView.measuredWidth, layoutHeight)

        val bitmap = Bitmap.createBitmap(outputWidth, outputHeight, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        if (!(transparent && output != "jpeg")) canvas.drawColor(Color.WHITE)
        canvas.scale(outputWidth.toFloat() / webView.measuredWidth.toFloat(), outputWidth.toFloat() / webView.measuredWidth.toFloat())
        webView.draw(canvas)

        val stream = ByteArrayOutputStream()
        val mimeType: String
        val extension: String
        val compressed = when (output) {
            "jpeg" -> {
                mimeType = "image/jpeg"
                extension = "jpg"
                bitmap.compress(Bitmap.CompressFormat.JPEG, quality, stream)
            }
            "webp" -> {
                mimeType = "image/webp"
                extension = "webp"
                bitmap.compress(Bitmap.CompressFormat.WEBP_LOSSY, quality, stream)
            }
            else -> {
                mimeType = "image/png"
                extension = "png"
                bitmap.compress(Bitmap.CompressFormat.PNG, 100, stream)
            }
        }
        bitmap.recycle()

        if (!compressed) {
            if (completed.compareAndSet(false, true)) {
                root.removeView(webView)
                webView.destroy()
                invoke.reject("Android could not encode the capture")
            }
            return
        }

        val bytes = stream.toByteArray()
        val id = UUID.randomUUID().toString()
        val host = Uri.parse(webView.url ?: "").host
            ?.replace(Regex("[^a-zA-Z0-9.-]"), "_")
            ?.take(80)
            ?: "capture"
        val name = "${host}_${System.currentTimeMillis()}.$extension"
        captures[id] = StoredCapture(bytes, name, mimeType)
        while (captures.size > MAX_HISTORY) captures.remove(captures.keys.first())

        if (completed.compareAndSet(false, true)) {
            root.removeView(webView)
            webView.stopLoading()
            webView.destroy()
            val result = JSObject()
            result.put("id", id)
            result.put("name", name)
            result.put("type", mimeType)
            result.put("width", outputWidth)
            result.put("height", outputHeight)
            result.put("dataUrl", "data:$mimeType;base64,${Base64.encodeToString(bytes, Base64.NO_WRAP)}")
            invoke.resolve(result)
        }
    }

    @Command
    fun save(invoke: Invoke) {
        val id = invoke.getArgs().optString("id", "")
        val capture = captures[id]
        if (capture == null) {
            invoke.reject("That capture is no longer available")
            return
        }

        try {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, capture.name)
                put(MediaStore.Downloads.MIME_TYPE, capture.mimeType)
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/ViperCapture")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = activity.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("Android could not create the download")
            resolver.openOutputStream(uri)?.use { it.write(capture.bytes) }
                ?: throw IllegalStateException("Android could not write the download")
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)

            val result = JSObject()
            result.put("name", capture.name)
            result.put("uri", uri.toString())
            invoke.resolve(result)
        } catch (error: Exception) {
            invoke.reject("Could not save the image: ${error.message}")
        }
    }

    @Command
    fun open_downloads(invoke: Invoke) {
        try {
            activity.startActivity(Intent(DownloadManager.ACTION_VIEW_DOWNLOADS))
            invoke.resolve()
        } catch (error: Exception) {
            invoke.reject("Could not open Downloads: ${error.message}")
        }
    }

    @Command
    fun open_external(invoke: Invoke) {
        val url = when (invoke.getArgs().optString("destination", "")) {
            "github" -> "https://github.com/Viperisuseful/ViperCapture"
            "cloud" -> "https://capture.viperisuseful.cc"
            else -> {
                invoke.reject("That external destination is not allowed")
                return
            }
        }
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            invoke.resolve()
        } catch (error: Exception) {
            invoke.reject("Could not open the link: ${error.message}")
        }
    }
}
