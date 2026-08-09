import { useEffect, useMemo, useRef, useState } from "react"
import { invoke } from "@tauri-apps/api/core"
import { getCurrentWindow } from "@tauri-apps/api/window"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Download,
  ExternalLink,
  FolderOpen,
  Gauge,
  Code2,
  Globe2,
  ImageIcon,
  Loader2,
  Minus,
  Moon,
  RotateCcw,
  Square,
  SlidersHorizontal,
  Sparkles,
  Sun,
  X,
  Zap,
} from "lucide-react"
import { toast } from "sonner"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupTextarea } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

type Output = "png" | "jpeg" | "webp" | "avif" | "pdf" | "html" | "markdown" | "metadata" | "webm" | "mp4" | "gif"
type BrowserEngine = "chromium" | "firefox" | "webkit"
type SourceType = "url" | "html" | "markdown"
type Device = "desktop" | "iphone_14" | "pixel_7" | "ipad"
type Capture = { id?: string; name: string; url: string; type: string; text?: string }
type AppConfig = {
  max_screenshot_pixels?: number
  max_viewport_width?: number
  max_viewport_height?: number
  max_full_page_height?: number
  browser_engines?: BrowserEngine[]
  output_formats?: Output[]
  gpu?: { mode?: "off" | "auto" | "required"; hardware_active?: boolean; mutable?: boolean }
}
type BackendConfig = { baseUrl: string; token: string }
type CaptchaWarning = { provider: string; requestId?: string }
type ThemePreference = "system" | "light" | "dark"

const appWindow = getCurrentWindow()
const themeStorageKey = "vipercapture.desktop.theme"
const isAndroid = /Android/i.test(navigator.userAgent)

const presets = [
  ["Phone", 390, 844],
  ["Tablet", 768, 1024],
  ["HD", 1280, 720],
  ["Full HD", 1920, 1080],
  ["2K", 2560, 1440],
  ["4K", 3840, 2160],
  ["8K", 7680, 4320],
] as const

const providerNames: Record<string, string> = {
  cloudflare: "Cloudflare",
  recaptcha: "Google reCAPTCHA",
  hcaptcha: "hCaptcha",
  funcaptcha: "Arkose Labs",
  datadome: "DataDome",
  unknown: "A page-level CAPTCHA",
}

const MAX_TEXT_PREVIEW_BYTES = 1024 * 1024

function extension(output: Output) {
  if (output === "jpeg") return "jpg"
  if (output === "markdown") return "md"
  if (output === "metadata") return "json"
  return output
}

function mergeObjects(base: Record<string, unknown>, overrides: Record<string, unknown>) {
  const result = { ...base }
  for (const [key, value] of Object.entries(overrides)) {
    const current = result[key]
    result[key] = value && current && !Array.isArray(value) && !Array.isArray(current) && typeof value === "object" && typeof current === "object"
      ? mergeObjects(current as Record<string, unknown>, value as Record<string, unknown>)
      : value
  }
  return result
}

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === "string" && error.trim()) return error
  return fallback
}

export default function App() {
  const [themePreference, setThemePreference] = useState<ThemePreference>(() => {
    const stored = localStorage.getItem(themeStorageKey)
    return stored === "light" || stored === "dark" ? stored : "system"
  })
  const [systemDark, setSystemDark] = useState(() => window.matchMedia("(prefers-color-scheme: dark)").matches)
  const [maximized, setMaximized] = useState(false)
  const [sourceType, setSourceType] = useState<SourceType>("url")
  const [url, setUrl] = useState("https://example.com")
  const [baseUrl, setBaseUrl] = useState("")
  const [engine, setEngine] = useState<BrowserEngine>("chromium")
  const [output, setOutput] = useState<Output>("png")
  const [width, setWidth] = useState(1280)
  const [height, setHeight] = useState(720)
  const [density, setDensity] = useState(1)
  const [fullPage, setFullPage] = useState(true)
  const [preserveViewportWidth, setPreserveViewportWidth] = useState(false)
  const [selector, setSelector] = useState("")
  const [quality, setQuality] = useState(90)
  const [transparent, setTransparent] = useState(false)
  const [lazyLoad, setLazyLoad] = useState("thorough")
  const [optimizePng, setOptimizePng] = useState(false)
  const [resizeWidth, setResizeWidth] = useState("")
  const [resizeHeight, setResizeHeight] = useState("")
  const [diagnostics, setDiagnostics] = useState(false)
  const [includeHar, setIncludeHar] = useState(false)
  const [includeTrace, setIncludeTrace] = useState(false)
  const [includeWarc, setIncludeWarc] = useState(false)
  const [videoDuration, setVideoDuration] = useState(5)
  const [videoScroll, setVideoScroll] = useState(false)
  const [waitEvent, setWaitEvent] = useState("load")
  const [waitDelay, setWaitDelay] = useState(1)
  const [waitSelector, setWaitSelector] = useState("")
  const [waitText, setWaitText] = useState("")
  const [waitTimeout, setWaitTimeout] = useState(15)
  const [headers, setHeaders] = useState("")
  const [consent, setConsent] = useState("reject")
  const [blockAds, setBlockAds] = useState(true)
  const [blockTrackers, setBlockTrackers] = useState(true)
  const [blockChats, setBlockChats] = useState(true)
  const [blockNewsletters, setBlockNewsletters] = useState(true)
  const [device, setDevice] = useState<Device>("desktop")
  const [colorScheme, setColorScheme] = useState("system")
  const [reducedMotion, setReducedMotion] = useState("system")
  const [locale, setLocale] = useState("")
  const [timezone, setTimezone] = useState("")
  const [customCss, setCustomCss] = useState("")
  const [failStatuses, setFailStatuses] = useState("")
  const [clipEnabled, setClipEnabled] = useState(false)
  const [clipX, setClipX] = useState(0)
  const [clipY, setClipY] = useState(0)
  const [clipWidth, setClipWidth] = useState(640)
  const [clipHeight, setClipHeight] = useState(480)
  const [extractMode, setExtractMode] = useState("document")
  const [includeShadowDom, setIncludeShadowDom] = useState(false)
  const [pdfMode, setPdfMode] = useState("print")
  const [paperSize, setPaperSize] = useState("A4")
  const [orientation, setOrientation] = useState("portrait")
  const [pdfMargin, setPdfMargin] = useState(0.4)
  const [pdfPageRanges, setPdfPageRanges] = useState("")
  const [pdfHeader, setPdfHeader] = useState("")
  const [pdfFooter, setPdfFooter] = useState("")
  const [deterministic, setDeterministic] = useState(false)
  const [sliceHeight, setSliceHeight] = useState("")
  const [sliceOverlap, setSliceOverlap] = useState(0)
  const [expertOverrides, setExpertOverrides] = useState("{}")
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [backend, setBackend] = useState<BackendConfig | null>(null)
  const [gpuBusy, setGpuBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState("Starting renderer")
  const [latest, setLatest] = useState<Capture | null>(null)
  const [history, setHistory] = useState<Capture[]>([])
  const [captchaWarning, setCaptchaWarning] = useState<CaptchaWarning | null>(null)
  const historyRef = useRef<Capture[]>([])

  const dark = themePreference === "system" ? systemDark : themePreference === "dark"

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)")
    const updateSystemTheme = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    media.addEventListener("change", updateSystemTheme)
    return () => media.removeEventListener("change", updateSystemTheme)
  }, [])
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
    document.documentElement.style.colorScheme = dark ? "dark" : "light"
    if (themePreference === "system") {
      localStorage.removeItem(themeStorageKey)
    } else {
      localStorage.setItem(themeStorageKey, themePreference)
    }
  }, [dark, themePreference])
  useEffect(() => {
    if (isAndroid) return
    let stopListening: (() => void) | undefined
    const syncWindowState = async () => setMaximized(await appWindow.isMaximized())

    void syncWindowState()
    void appWindow.onResized(() => {
      void syncWindowState()
    }).then((unlisten) => {
      stopListening = unlisten
    })

    return () => stopListening?.()
  }, [])
  useEffect(() => {
    let cancelled = false

    const start = async () => {
      if (isAndroid) {
        setConfig({ max_screenshot_pixels: 16_000_000, max_viewport_width: 7680, max_viewport_height: 4320, gpu: { mode: "off", mutable: false } })
        setStatus("Ready")
        return
      }
      try {
        const runtime = await invoke<BackendConfig>("backend_config")
        for (let attempt = 0; attempt < 120; attempt += 1) {
          try {
            const response = await fetch(`${runtime.baseUrl}/health`, {
              headers: { Authorization: `Bearer ${runtime.token}` },
            })
            if (response.ok) {
              const appConfig = await fetch(`${runtime.baseUrl}/app-config`, {
                headers: { Authorization: `Bearer ${runtime.token}` },
              })
              if (!appConfig.ok) throw new Error("Renderer configuration failed")
              if (!cancelled) {
                setBackend(runtime)
                setConfig(await appConfig.json())
                setStatus("Ready")
              }
              return
            }
          } catch {
            // The sidecar binds its port after Chromium has initialized.
          }
          await new Promise((resolve) => window.setTimeout(resolve, 250))
        }
        throw new Error("The local renderer did not become ready")
      } catch (error) {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : String(error)
          setStatus("Renderer unavailable")
          toast.error(message)
        }
      }
    }

    void start()
    return () => {
      cancelled = true
    }
  }, [])
  useEffect(
    () => () => historyRef.current.forEach((item) => {
      if (item.url.startsWith("blob:")) URL.revokeObjectURL(item.url)
    }),
    [],
  )

  const maxPixels = config?.max_screenshot_pixels ?? 500_000_000
  const maxWidth = config?.max_viewport_width ?? 16_384
  const maxHeight = config?.max_viewport_height ?? 16_384
  const gpuEnabled = config?.gpu?.mode !== "off"
  const gpuCopy = useMemo(() => {
    if (!gpuEnabled) return "Uses Chromium's default software-compatible mode."
    return config?.gpu?.hardware_active
      ? "Hardware compositing verified by Chromium."
      : "GPU requested; hardware acceleration was not verified."
  }, [config, gpuEnabled])

  const fits = (w: number, h: number, scale = density) =>
    w * h * scale * scale <= maxPixels

  const backendFetch = (path: string, init: RequestInit = {}) => {
    if (!backend) throw new Error("The local renderer is still starting")
    const requestHeaders = new Headers(init.headers)
    requestHeaders.set("Authorization", `Bearer ${backend.token}`)
    return fetch(`${backend.baseUrl}${path}`, { ...init, headers: requestHeaders })
  }

  const openExternal = async (destination: "github" | "cloud") => {
    try {
      await invoke(isAndroid ? "plugin:mobile-capture|open_external" : "open_external", { destination })
    } catch (error) {
      toast.error(errorMessage(error, "Could not open the link"))
    }
  }

  const downloadCapture = async (item: Capture) => {
    if (isAndroid) {
      if (!item.id) return toast.error("That capture is no longer available")
      try {
        const saved = await invoke<{ name: string }>("plugin:mobile-capture|save", { id: item.id })
        toast.success("Image saved to Downloads", { description: saved.name })
      } catch (error) {
        toast.error(errorMessage(error, "Could not save the image"))
      }
      return
    }
    const link = document.createElement("a")
    link.href = item.url
    link.download = item.name
    link.click()
    toast.success("Image downloaded", { description: item.name })
  }

  const openDownloads = async () => {
    try {
      await invoke(isAndroid ? "plugin:mobile-capture|open_downloads" : "open_downloads")
    } catch (error) {
      toast.error(errorMessage(error, "Could not open Downloads"))
    }
  }

  const reset = () => {
    setSourceType("url")
    setBaseUrl("")
    setEngine("chromium")
    setOutput("png")
    setWidth(1280)
    setHeight(720)
    setDensity(1)
    setFullPage(true)
    setPreserveViewportWidth(false)
    setSelector("")
    setQuality(90)
    setTransparent(false)
    setLazyLoad("thorough")
    setOptimizePng(false)
    setResizeWidth("")
    setResizeHeight("")
    setDiagnostics(false)
    setIncludeHar(false)
    setIncludeTrace(false)
    setIncludeWarc(false)
    setVideoDuration(5)
    setVideoScroll(false)
    setWaitEvent("load")
    setWaitDelay(1)
    setWaitSelector("")
    setWaitText("")
    setWaitTimeout(15)
    setHeaders("")
    setConsent("reject")
    setBlockAds(true)
    setBlockTrackers(true)
    setBlockChats(true)
    setBlockNewsletters(true)
    setDevice("desktop")
    setColorScheme("system")
    setReducedMotion("system")
    setLocale("")
    setTimezone("")
    setCustomCss("")
    setFailStatuses("")
    setClipEnabled(false)
    setClipX(0)
    setClipY(0)
    setClipWidth(640)
    setClipHeight(480)
    setExtractMode("document")
    setIncludeShadowDom(false)
    setPdfMode("print")
    setPaperSize("A4")
    setOrientation("portrait")
    setPdfMargin(0.4)
    setPdfPageRanges("")
    setPdfHeader("")
    setPdfFooter("")
    setDeterministic(false)
    setSliceHeight("")
    setSliceOverlap(0)
    setExpertOverrides("{}")
  }

  const setGpu = async (enabled: boolean) => {
    if (!config?.gpu?.mutable || gpuBusy) return
    setGpuBusy(true)
    try {
      const response = await backendFetch("/local/gpu-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: enabled ? "auto" : "off" }),
      })
      const body = await response.json().catch(() => null)
      if (!response.ok) throw new Error(body?.detail ?? "Could not change GPU mode")
      setConfig((current) => ({ ...current, gpu: body.gpu }))
      toast.success(
        body.gpu.hardware_active
          ? "GPU rendering enabled and verified"
          : enabled
            ? "GPU mode enabled; Chromium is using a software fallback"
            : "GPU rendering disabled",
      )
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not change GPU mode")
    } finally {
      setGpuBusy(false)
    }
  }

  const capture = async (proceedOnCaptcha = false) => {
    if (!url.trim()) return toast.error(`Enter ${sourceType === "url" ? "a public website URL" : sourceType.toUpperCase()}.`)
    if (!fits(width, height)) return toast.error("The selected viewport and density exceed the server pixel limit.")
    if (new TextEncoder().encode(customCss).length > 64 * 1024) {
      return toast.error("Custom CSS may use at most 64 KiB.")
    }
    const parsedStatuses = failStatuses.trim()
      ? failStatuses.split(",").map((value) => Number(value.trim()))
      : []
    if (
      parsedStatuses.some((value) => !Number.isInteger(value) || value < 100 || value > 599) ||
      new Set(parsedStatuses).size !== parsedStatuses.length
    ) {
      return toast.error("Failure statuses must be unique HTTP codes from 100 through 599.")
    }
    let customHeaders: Record<string, string> = {}
    if (headers.trim()) {
      try {
        customHeaders = JSON.parse(headers)
      } catch {
        return toast.error("Custom headers must be a JSON object.")
      }
      if (!customHeaders || Array.isArray(customHeaders) || typeof customHeaders !== "object") {
        return toast.error("Custom headers must be a JSON object.")
      }
      if (Object.values(customHeaders).some((value) => typeof value !== "string")) {
        return toast.error("Every custom header value must be text.")
      }
      if (sourceType !== "url" && !baseUrl.trim()) {
        return toast.error("A base URL is required for raw-input custom headers.")
      }
    }
    let overrides: Record<string, unknown>
    try {
      overrides = JSON.parse(expertOverrides)
      if (!overrides || Array.isArray(overrides) || typeof overrides !== "object") throw new Error()
    } catch {
      return toast.error("Expert overrides must be a JSON object.")
    }
    const normalized = sourceType === "url" && !/^https?:\/\//i.test(url.trim()) ? `https://${url.trim()}` : url.trim()
    const imageOutput = ["png", "jpeg", "webp", "avif"].includes(output)
    const documentOutput = ["html", "markdown"].includes(output)
    const videoOutput = ["webm", "mp4", "gif"].includes(output)
    const effectiveFullPage = imageOutput && !selector && !clipEnabled && fullPage
    if (effectiveFullPage && sliceHeight && (resizeWidth || resizeHeight)) {
      return toast.error("Image resizing and sliced output cannot be combined.")
    }
    if (effectiveFullPage && sliceHeight && sliceOverlap >= Number(sliceHeight)) {
      return toast.error("Slice overlap must be smaller than slice height.")
    }
    const source = sourceType === "url"
      ? { url: normalized }
      : { [sourceType]: normalized, base_url: baseUrl.trim() || null }
    const basePayload: Record<string, unknown> = {
      ...source,
      engine,
      output,
      viewport: { width, height, device_scale_factor: density },
      environment: {
        device,
        color_scheme: colorScheme === "system" ? null : colorScheme,
        reduced_motion: reducedMotion === "system" ? null : reducedMotion,
        locale: locale.trim() || null,
        timezone: timezone.trim() || null,
      },
      full_page: imageOutput ? effectiveFullPage : true,
      preserve_viewport_width: effectiveFullPage && preserveViewportWidth,
      lazy_load: lazyLoad,
      selector: imageOutput && !clipEnabled ? selector || null : null,
      clip: imageOutput && clipEnabled ? { x: clipX, y: clipY, width: clipWidth, height: clipHeight } : null,
      custom_css: customCss || null,
      fail_on_status: parsedStatuses,
      image: {
        quality: ["jpeg", "webp", "avif"].includes(output) ? quality : null,
        width: imageOutput && !sliceHeight && resizeWidth ? Number(resizeWidth) : null,
        height: imageOutput && !sliceHeight && resizeHeight ? Number(resizeHeight) : null,
        transparent_background: ["png", "webp", "avif"].includes(output) && transparent,
        optimize_for_speed: engine === "chromium" && (output === "png" || output === "webp") && optimizePng,
      },
      video: videoOutput
        ? { duration_ms: videoDuration * 1000, scroll: videoScroll }
        : null,
      pdf: output === "pdf" ? {
        mode: pdfMode,
        paper_size: paperSize,
        orientation,
        margins: { top: pdfMargin, right: pdfMargin, bottom: pdfMargin, left: pdfMargin },
        page_ranges: pdfMode === "print" ? pdfPageRanges.trim() || null : null,
        header_template: pdfHeader.trim() || null,
        footer_template: pdfFooter.trim() || null,
      } : null,
      extract_mode: documentOutput ? extractMode : "document",
      include_shadow_dom: documentOutput && includeShadowDom,
      slices: effectiveFullPage && sliceHeight ? { height: Number(sliceHeight), overlap: sliceOverlap } : null,
      diagnostics: {
        bundle: diagnostics,
        include_console: true,
        include_network: true,
        include_har: diagnostics && includeHar,
        include_trace: diagnostics && !videoOutput && includeTrace,
        include_warc: diagnostics && includeWarc,
      },
      deterministic: { enabled: deterministic },
      headers: customHeaders,
      wait_for: {
        event: waitEvent,
        selector: waitSelector || null,
        text: waitText || null,
        delay_ms: waitDelay * 1000,
        timeout_ms: waitTimeout * 1000,
      },
      cleanup: {
        consent_mode: consent,
        block_ads: blockAds,
        block_trackers: blockTrackers,
        block_chats: blockChats,
        block_newsletters: blockNewsletters,
      },
      proceed_on_captcha: proceedOnCaptcha,
    }
    const payload = mergeObjects(basePayload, overrides)
    setBusy(true)
    setStatus("Rendering")
    try {
      if (isAndroid) {
        const rendered = await invoke<{
          id: string
          name: string
          type: string
          dataUrl: string
        }>("plugin:mobile-capture|capture", {
          url: normalized,
          output,
          width,
          height,
          density,
          fullPage,
          quality,
          transparent,
          lazyLoad,
          waitDelayMs: waitDelay * 1000,
          timeoutMs: waitTimeout * 1000,
        })
        const item = {
          id: rendered.id,
          name: rendered.name,
          url: rendered.dataUrl,
          type: rendered.type,
        }
        setLatest(item)
        setHistory((items) => {
          const next = [item, ...items].slice(0, 6)
          historyRef.current = next
          return next
        })
        setStatus("Complete")
        toast.success("Capture ready")
        return
      }
      const response = await backendFetch("/v1/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => null)
        const requestId = body?.error?.request_id ?? response.headers.get("x-request-id")
        if (response.status === 409 && body?.error?.code === "captcha_detected") {
          const provider = body?.error?.details?.provider ?? "unknown"
          setCaptchaWarning({ provider: providerNames[provider] ?? provider, requestId })
          setStatus("Challenge detected")
          return
        }
        const message = body?.detail ?? body?.error?.message ?? `Capture failed (${response.status})`
        throw new Error(requestId ? `${message} · Request ${requestId}` : message)
      }
      const blob = await response.blob()
      const disposition = response.headers.get("content-disposition") ?? ""
      const serverName = /filename="?([^";]+)"?/i.exec(disposition)?.[1]
      const host = (() => {
        try {
          return new URL(normalized).hostname.replace(/[^a-z0-9.-]+/gi, "_")
        } catch {
          return "capture"
        }
      })()
      const text = (blob.type.startsWith("text/") || blob.type === "application/json") && blob.size <= MAX_TEXT_PREVIEW_BYTES
        ? await blob.text()
        : undefined
      const item = {
        name: serverName ?? `${host}_${Date.now()}.${extension(output)}`,
        url: URL.createObjectURL(blob),
        type: blob.type,
        text,
      }
      setLatest(item)
      setHistory((items) => {
        const next = [item, ...items].slice(0, 6)
        items.slice(5).forEach((dropped) => URL.revokeObjectURL(dropped.url))
        historyRef.current = next
        return next
      })
      setStatus("Complete")
      toast.success("Capture ready")
    } catch (error) {
      setStatus("Failed")
      toast.error(errorMessage(error, "Capture failed"))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-svh bg-background text-foreground">
      <header className={`app-titlebar sticky top-0 z-50 flex select-none items-center border-b bg-background/95 backdrop-blur ${isAndroid ? "android-titlebar" : "h-12"}`} data-tauri-drag-region={!isAndroid ? true : undefined}>
        <div className="flex min-w-0 items-center gap-2 px-4 font-semibold tracking-tight" data-tauri-drag-region>
          <img src={`${import.meta.env.BASE_URL}vipercapture-mark.svg`} alt="" className="size-7 rounded-md" draggable={false} />
          <span data-tauri-drag-region>ViperCapture</span>
          <Badge variant="secondary" className="hidden sm:inline-flex" data-tauri-drag-region>Open source</Badge>
        </div>
        <div className="h-full min-w-4 flex-1" data-tauri-drag-region />
        <nav className="flex h-full items-center">
          <Button
            variant="ghost"
            size="icon-sm"
            className="mr-1"
            onClick={() => setThemePreference(dark ? "light" : "dark")}
            aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
            title={themePreference === "system" ? "Using system theme" : dark ? "Switch to light theme" : "Switch to dark theme"}
          >
            {dark ? <Sun /> : <Moon />}
          </Button>
          <Button variant="ghost" size="sm" className="mr-2" onClick={() => void openExternal("github")}>
            <Code2 data-icon="inline-start" />
            <span className="hidden sm:inline">GitHub</span>
          </Button>
          {!isAndroid && (
            <>
              <button className="window-control" onClick={() => void appWindow.minimize()} aria-label="Minimize window" title="Minimize">
                <Minus aria-hidden="true" />
              </button>
              <button
                className="window-control"
                onClick={() => void appWindow.toggleMaximize()}
                aria-label={maximized ? "Restore window" : "Maximize window"}
                title={maximized ? "Restore" : "Maximize"}
              >
                <Square aria-hidden="true" className={maximized ? "size-3" : undefined} />
              </button>
              <button className="window-control window-control-close" onClick={() => void appWindow.close()} aria-label="Close window" title="Close">
                <X aria-hidden="true" />
              </button>
            </>
          )}
        </nav>
      </header>

      <main className="site-shell py-10 sm:py-14">
        <section className="mb-8 grid gap-6 lg:grid-cols-[1fr_auto] lg:items-end">
          <div className="max-w-3xl">
            <Badge variant="outline" className="mb-4"><Sparkles data-icon="inline-start" />{isAndroid ? "Local Android renderer" : "Local cross-browser renderer"}</Badge>
            <h1 className="page-title">The whole webpage, in one capture.</h1>
            <p className="mt-4 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">
              Render URLs, HTML, or Markdown with the complete ViperCapture engine on your machine.
            </p>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {[
              [Gauge, "Local", "Private"],
              [Zap, "Fast", isAndroid ? "WebView" : "3 engines"],
              [ImageIcon, isAndroid ? "3" : String(config?.output_formats?.length ?? 10), "Formats"],
            ].map(([Icon, value, label]) => (
              <Card key={String(label)} size="sm" className="min-w-24">
                <CardContent className="flex items-center gap-2">
                  <Icon />
                  <div><p className="text-sm font-medium">{String(value)}</p><p className="text-xs text-muted-foreground">{String(label)}</p></div>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>

        <section className="hairline-panel overflow-hidden">
          <div className="flex flex-col gap-4 border-b bg-card p-4 lg:flex-row lg:items-end">
            <FieldGroup className="min-w-0 flex-1 gap-2">
              {!isAndroid && <ToggleGroup type="single" value={sourceType} onValueChange={(value) => value && setSourceType(value as SourceType)} variant="outline">
                <ToggleGroupItem value="url">URL</ToggleGroupItem><ToggleGroupItem value="html">HTML</ToggleGroupItem><ToggleGroupItem value="markdown">Markdown</ToggleGroupItem>
              </ToggleGroup>}
              <Field>
                <FieldLabel htmlFor="capture-source">{sourceType === "url" ? "Website URL" : sourceType.toUpperCase()}</FieldLabel>
                <InputGroup className="h-auto min-h-10">
                  <InputGroupAddon><Globe2 /></InputGroupAddon>
                  <InputGroupTextarea id="capture-source" rows={sourceType === "url" ? 1 : 5} value={url} onChange={(event) => setUrl(event.target.value)} placeholder={sourceType === "url" ? "https://example.com" : sourceType === "html" ? "<main>Hello</main>" : "# Hello"} className="min-h-10" />
                </InputGroup>
              </Field>
              {!isAndroid && sourceType !== "url" && <Field><FieldLabel htmlFor="base-url">Base URL (optional)</FieldLabel><Input id="base-url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://example.com/" /></Field>}
            </FieldGroup>
            <Button size="lg" onClick={() => void capture()} disabled={busy || (!isAndroid && !backend)} className="lg:min-w-36">
              {busy ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <ImageIcon data-icon="inline-start" />}
              {busy ? "Rendering" : "Capture"}
            </Button>
          </div>

          <div className="grid min-h-[650px] lg:grid-cols-[380px_minmax(0,1fr)]">
            <aside className="border-b bg-muted/25 p-4 lg:border-r lg:border-b-0">
              <div className="mb-5 flex items-center justify-between">
                <div><p className="font-medium">Capture settings</p><p className="text-xs text-muted-foreground">Runs on your machine</p></div>
                <Button size="icon-sm" variant="ghost" onClick={reset} aria-label="Reset settings"><RotateCcw /></Button>
              </div>
              <FieldGroup>
                <FieldSet>
                  <FieldLegend>Output</FieldLegend>
                  <Field>
                    {isAndroid ? <Tabs value={output} onValueChange={(value: string) => setOutput(value as Output)}>
                      <TabsList className="w-full"><TabsTrigger value="png">PNG</TabsTrigger><TabsTrigger value="jpeg">JPEG</TabsTrigger><TabsTrigger value="webp">WebP</TabsTrigger></TabsList>
                    </Tabs> : <Select value={output} onValueChange={(value) => { setOutput(value as Output); if (value === "pdf") setEngine("chromium") }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
                      {(config?.output_formats ?? ["png", "jpeg", "webp", "avif", "pdf", "html", "markdown", "metadata", "webm", "gif"]).map((format) => <SelectItem key={format} value={format}>{format.toUpperCase()}</SelectItem>)}
                    </SelectGroup></SelectContent></Select>}
                    {!isAndroid && <FieldDescription>MP4 is available when the local FFmpeg build includes libx264.</FieldDescription>}
                  </Field>
                </FieldSet>

                {!isAndroid && <FieldSet>
                  <FieldLegend>Browser engine</FieldLegend>
                  <Field data-disabled={output === "pdf"}><Select value={engine} onValueChange={(value) => setEngine(value as BrowserEngine)} disabled={output === "pdf"}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
                    {(config?.browser_engines ?? ["chromium", "firefox", "webkit"]).map((browser) => <SelectItem key={browser} value={browser}>{browser[0].toUpperCase() + browser.slice(1)}</SelectItem>)}
                  </SelectGroup></SelectContent></Select><FieldDescription>Firefox and WebKit start on first use. PDF and fast encoding require Chromium.</FieldDescription></Field>
                </FieldSet>}

                <FieldSet>
                  <FieldLegend>Viewport</FieldLegend>
                  <div className="grid grid-cols-3 gap-3">
                    <Field><FieldLabel htmlFor="width">Width</FieldLabel><Input id="width" type="number" min={1} max={maxWidth} value={width} onChange={(event) => setWidth(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="height">Height</FieldLabel><Input id="height" type="number" min={1} max={maxHeight} value={height} onChange={(event) => setHeight(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="density">Density</FieldLabel><Input id="density" type="number" min={0.1} max={isAndroid ? 4 : 8} step={0.25} value={density} onChange={(event) => setDensity(Number(event.target.value))} /></Field>
                  </div>
                  <ToggleGroup type="single" variant="outline" value={`${width}x${height}`} onValueChange={(value) => {
                    const preset = presets.find((item) => `${item[1]}x${item[2]}` === value)
                    if (preset) { setWidth(preset[1]); setHeight(preset[2]) }
                  }} className="grid grid-cols-3">
                    {presets.map(([label, presetWidth, presetHeight]) => (
                      <ToggleGroupItem key={label} value={`${presetWidth}x${presetHeight}`} disabled={!fits(presetWidth, presetHeight)} className="h-auto py-2 text-xs">
                        {label}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                  {["png", "jpeg", "webp", "avif"].includes(output) && <Field orientation="horizontal">
                    <FieldLabel htmlFor="full-page"><span><span className="block">Full page</span><FieldDescription>Scroll and capture the document.</FieldDescription></span></FieldLabel>
                    <Switch id="full-page" checked={fullPage} onCheckedChange={(checked: boolean) => setFullPage(checked)} disabled={Boolean(selector) || clipEnabled} />
                  </Field>}
                  {!isAndroid && ["png", "jpeg", "webp", "avif"].includes(output) && fullPage && !selector && !clipEnabled && <Field orientation="horizontal"><FieldLabel htmlFor="preserve-width"><span><span className="block">Preserve viewport width</span><FieldDescription>Clip horizontal overflow while retaining full height.</FieldDescription></span></FieldLabel><Switch id="preserve-width" checked={preserveViewportWidth} onCheckedChange={setPreserveViewportWidth} /></Field>}
                </FieldSet>

                {!isAndroid && (
                  <FieldSet>
                    <FieldLegend>Rendering</FieldLegend>
                    <Field orientation="horizontal" data-disabled={!config?.gpu?.mutable}>
                      <FieldLabel htmlFor="gpu-rendering">
                        <span>
                          <span className="flex items-center gap-2"><Zap />GPU rendering</span>
                          <FieldDescription>{gpuCopy}</FieldDescription>
                        </span>
                      </FieldLabel>
                      <Switch id="gpu-rendering" checked={gpuEnabled} onCheckedChange={(checked: boolean) => void setGpu(checked)} disabled={!config?.gpu?.mutable || gpuBusy} />
                    </Field>
                  </FieldSet>
                )}

                {!isAndroid && <FieldSet>
                  <FieldLegend>Page cleanup</FieldLegend>
                  <Field><FieldLabel>Cookie consent</FieldLabel><Select value={consent} onValueChange={setConsent}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="reject">Reject non-essential</SelectItem><SelectItem value="accept">Accept</SelectItem><SelectItem value="hide">Hide banner</SelectItem><SelectItem value="none">Leave unchanged</SelectItem></SelectGroup></SelectContent></Select></Field>
                  {[["cleanup-ads", "Ads", blockAds, setBlockAds], ["cleanup-trackers", "Trackers", blockTrackers, setBlockTrackers], ["cleanup-chats", "Chat widgets", blockChats, setBlockChats], ["cleanup-newsletters", "Newsletters", blockNewsletters, setBlockNewsletters]].map(([id, label, checked, setter]) => <Field key={String(id)} orientation="horizontal"><FieldLabel htmlFor={String(id)}>{String(label)}</FieldLabel><Switch id={String(id)} checked={checked as boolean} onCheckedChange={setter as (value: boolean) => void} /></Field>)}
                </FieldSet>}

                <Collapsible>
                  <CollapsibleTrigger asChild>
                    <Button variant="outline" className="w-full justify-between"><span className="flex items-center gap-2"><SlidersHorizontal />Advanced controls</span><ChevronDown /></Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-4">
                    <FieldGroup>
                      {!isAndroid && <FieldSet><FieldLegend>Deterministic environment</FieldLegend>
                        <Field><FieldLabel>Device signals</FieldLabel><Select value={device} onValueChange={(value) => setDevice(value as Device)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="desktop">Desktop Chromium</SelectItem><SelectItem value="iphone_14">iPhone 14</SelectItem><SelectItem value="pixel_7">Pixel 7</SelectItem><SelectItem value="ipad">iPad</SelectItem></SelectGroup></SelectContent></Select></Field>
                        <div className="grid grid-cols-2 gap-3"><Field><FieldLabel>Color scheme</FieldLabel><Select value={colorScheme} onValueChange={setColorScheme}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="system">Browser default</SelectItem><SelectItem value="light">Light</SelectItem><SelectItem value="dark">Dark</SelectItem><SelectItem value="no-preference">No preference</SelectItem></SelectGroup></SelectContent></Select></Field><Field><FieldLabel>Reduced motion</FieldLabel><Select value={reducedMotion} onValueChange={setReducedMotion}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="system">Browser default</SelectItem><SelectItem value="reduce">Reduce</SelectItem><SelectItem value="no-preference">No preference</SelectItem></SelectGroup></SelectContent></Select></Field></div>
                        <div className="grid grid-cols-2 gap-3"><Field><FieldLabel htmlFor="locale">Locale</FieldLabel><Input id="locale" value={locale} onChange={(event) => setLocale(event.target.value)} placeholder="en-US" /></Field><Field><FieldLabel htmlFor="timezone">IANA timezone</FieldLabel><Input id="timezone" value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="America/New_York" /></Field></div>
                      </FieldSet>}
                      {!isAndroid && ["png", "jpeg", "webp", "avif"].includes(output) && <Field><FieldLabel htmlFor="selector">Element selector</FieldLabel><Input id="selector" value={selector} onChange={(event) => { setSelector(event.target.value); if (event.target.value) setClipEnabled(false) }} placeholder="main, #invoice" /></Field>}
                      {!isAndroid && ["png", "jpeg", "webp", "avif"].includes(output) && <Field orientation="horizontal"><FieldLabel htmlFor="clip-enabled"><span><span className="block">Rectangular crop</span><FieldDescription>Crop CSS-pixel coordinates from the final document.</FieldDescription></span></FieldLabel><Switch id="clip-enabled" checked={clipEnabled} onCheckedChange={(checked) => { setClipEnabled(checked); if (checked) setSelector("") }} /></Field>}
                      {!isAndroid && ["png", "jpeg", "webp", "avif"].includes(output) && clipEnabled && <FieldGroup className="grid grid-cols-4 gap-2"><Field><FieldLabel htmlFor="clip-x">X</FieldLabel><Input id="clip-x" type="number" min={0} max={100000} value={clipX} onChange={(event) => setClipX(Number(event.target.value))} /></Field><Field><FieldLabel htmlFor="clip-y">Y</FieldLabel><Input id="clip-y" type="number" min={0} max={100000} value={clipY} onChange={(event) => setClipY(Number(event.target.value))} /></Field><Field><FieldLabel htmlFor="clip-width">Width</FieldLabel><Input id="clip-width" type="number" min={1} max={100000} value={clipWidth} onChange={(event) => setClipWidth(Number(event.target.value))} /></Field><Field><FieldLabel htmlFor="clip-height">Height</FieldLabel><Input id="clip-height" type="number" min={1} max={100000} value={clipHeight} onChange={(event) => setClipHeight(Number(event.target.value))} /></Field></FieldGroup>}
                      {!isAndroid && <Field><FieldLabel htmlFor="custom-css">Custom CSS</FieldLabel><InputGroup><InputGroupTextarea id="custom-css" rows={4} value={customCss} onChange={(event) => setCustomCss(event.target.value)} placeholder="header, .cookie-banner { display: none !important; }" /></InputGroup><FieldDescription>Applied to the main document; maximum 64 KiB.</FieldDescription></Field>}
                      {!isAndroid && <Field><FieldLabel htmlFor="fail-statuses">Fail on HTTP status</FieldLabel><Input id="fail-statuses" value={failStatuses} onChange={(event) => setFailStatuses(event.target.value)} placeholder="404,429,500,502,503" /></Field>}
                      <Field><FieldLabel>Lazy content loading</FieldLabel><Select value={lazyLoad} onValueChange={setLazyLoad}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="thorough">Thorough (default)</SelectItem><SelectItem value="adaptive">Adaptive (faster)</SelectItem><SelectItem value="none">None (fastest)</SelectItem></SelectGroup></SelectContent></Select></Field>
                      {!isAndroid && ["png", "jpeg", "webp", "avif"].includes(output) && <FieldSet><FieldLegend>Image delivery</FieldLegend>
                        <FieldGroup className="grid grid-cols-2 gap-3"><Field><FieldLabel htmlFor="resize-width">Maximum width</FieldLabel><Input id="resize-width" type="number" min={1} max={65535} value={resizeWidth} onChange={(event) => { setResizeWidth(event.target.value); if (event.target.value) setSliceHeight("") }} placeholder="Original" /></Field><Field><FieldLabel htmlFor="resize-height">Maximum height</FieldLabel><Input id="resize-height" type="number" min={1} max={65535} value={resizeHeight} onChange={(event) => { setResizeHeight(event.target.value); if (event.target.value) setSliceHeight("") }} placeholder="Original" /></Field></FieldGroup>
                        {fullPage && !selector && !clipEnabled && <FieldGroup className="grid grid-cols-2 gap-3"><Field><FieldLabel htmlFor="slice-height">Slice height</FieldLabel><Input id="slice-height" type="number" min={100} max={10000} value={sliceHeight} onChange={(event) => { setSliceHeight(event.target.value); if (event.target.value) { setResizeWidth(""); setResizeHeight("") } }} placeholder="No slices" /></Field><Field><FieldLabel htmlFor="slice-overlap">Overlap</FieldLabel><Input id="slice-overlap" type="number" min={0} max={sliceHeight ? Math.min(1000, Number(sliceHeight) - 1) : 1000} value={sliceOverlap} onChange={(event) => setSliceOverlap(Number(event.target.value))} /></Field></FieldGroup>}
                      </FieldSet>}
                      {!isAndroid && ["html", "markdown"].includes(output) && <FieldSet><FieldLegend>Document extraction</FieldLegend>
                        <Field><FieldLabel>Extraction mode</FieldLabel><Select value={extractMode} onValueChange={setExtractMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="document">Complete document</SelectItem><SelectItem value="article">Readable article</SelectItem></SelectGroup></SelectContent></Select></Field>
                        <Field orientation="horizontal"><FieldLabel htmlFor="shadow-dom"><span><span className="block">Include open shadow DOM</span><FieldDescription>Serializes open component roots as declarative shadow DOM.</FieldDescription></span></FieldLabel><Switch id="shadow-dom" checked={includeShadowDom} onCheckedChange={setIncludeShadowDom} /></Field>
                      </FieldSet>}
                      {!isAndroid && output === "pdf" && <FieldSet><FieldLegend>PDF</FieldLegend>
                        <FieldGroup className="grid grid-cols-2 gap-3"><Field><FieldLabel>Mode</FieldLabel><Select value={pdfMode} onValueChange={setPdfMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="print">Print pages</SelectItem><SelectItem value="single_page">Single page</SelectItem></SelectGroup></SelectContent></Select></Field><Field><FieldLabel>Paper</FieldLabel><Select value={paperSize} onValueChange={setPaperSize}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["A0", "A1", "A2", "A3", "A4", "A5", "A6", "Legal", "Letter", "Tabloid"].map((size) => <SelectItem key={size} value={size}>{size}</SelectItem>)}</SelectGroup></SelectContent></Select></Field></FieldGroup>
                        <FieldGroup className="grid grid-cols-2 gap-3"><Field><FieldLabel>Orientation</FieldLabel><Select value={orientation} onValueChange={setOrientation}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="portrait">Portrait</SelectItem><SelectItem value="landscape">Landscape</SelectItem></SelectGroup></SelectContent></Select></Field><Field><FieldLabel htmlFor="pdf-margin">Margins (in)</FieldLabel><Input id="pdf-margin" type="number" min={0} max={4} step={0.1} value={pdfMargin} onChange={(event) => setPdfMargin(Number(event.target.value))} /></Field></FieldGroup>
                        {pdfMode === "print" && <Field><FieldLabel htmlFor="page-ranges">Page ranges</FieldLabel><Input id="page-ranges" value={pdfPageRanges} onChange={(event) => setPdfPageRanges(event.target.value)} placeholder="1-3, 5" /></Field>}
                        <Field><FieldLabel htmlFor="pdf-header">Header template</FieldLabel><InputGroup><InputGroupTextarea id="pdf-header" rows={2} value={pdfHeader} onChange={(event) => setPdfHeader(event.target.value)} placeholder={'<span class="title"></span>'} /></InputGroup></Field>
                        <Field><FieldLabel htmlFor="pdf-footer">Footer template</FieldLabel><InputGroup><InputGroupTextarea id="pdf-footer" rows={2} value={pdfFooter} onChange={(event) => setPdfFooter(event.target.value)} placeholder={'<span class="pageNumber"></span>'} /></InputGroup></Field>
                      </FieldSet>}
                      {!isAndroid && ["webm", "mp4", "gif"].includes(output) && <FieldSet><FieldLegend>Video</FieldLegend><Field><FieldLabel htmlFor="video-duration">Duration (seconds)</FieldLabel><Input id="video-duration" type="number" min={1} max={30} value={videoDuration} onChange={(event) => setVideoDuration(Number(event.target.value))} /></Field><Field orientation="horizontal"><FieldLabel htmlFor="video-scroll">Scroll while recording</FieldLabel><Switch id="video-scroll" checked={videoScroll} onCheckedChange={setVideoScroll} /></Field></FieldSet>}
                      {!isAndroid && <FieldSet><FieldLegend>Evidence and reproducibility</FieldLegend>
                        <Field orientation="horizontal"><FieldLabel htmlFor="deterministic">Deterministic time, randomness, motion, and fonts</FieldLabel><Switch id="deterministic" checked={deterministic} onCheckedChange={setDeterministic} /></Field>
                        <Field orientation="horizontal"><FieldLabel htmlFor="diagnostics">Diagnostic ZIP</FieldLabel><Switch id="diagnostics" checked={diagnostics} onCheckedChange={setDiagnostics} /></Field>
                        {diagnostics && <FieldGroup className="gap-3">{[["har", "HAR", includeHar, setIncludeHar], ...(["webm", "mp4", "gif"].includes(output) ? [] : [["trace", "Playwright trace", includeTrace, setIncludeTrace]]), ["warc", "WARC", includeWarc, setIncludeWarc]].map(([id, label, checked, setter]) => <Field key={String(id)} orientation="horizontal"><FieldLabel htmlFor={String(id)}>{String(label)}</FieldLabel><Switch id={String(id)} checked={checked as boolean} onCheckedChange={setter as (value: boolean) => void} /></Field>)}</FieldGroup>}
                      </FieldSet>}
                      <div className="grid grid-cols-3 gap-3">
                        {!isAndroid && <Field><FieldLabel>Load event</FieldLabel><Select value={waitEvent} onValueChange={setWaitEvent}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="load">Load</SelectItem><SelectItem value="domcontentloaded">DOM ready</SelectItem><SelectItem value="networkidle">Network idle</SelectItem></SelectGroup></SelectContent></Select></Field>}
                        <Field><FieldLabel htmlFor="delay">Wait (sec)</FieldLabel><Input id="delay" type="number" min={0} max={15} value={waitDelay} onChange={(event) => setWaitDelay(Number(event.target.value))} /></Field>
                        <Field><FieldLabel htmlFor="timeout">Timeout</FieldLabel><Input id="timeout" type="number" min={1} max={30} value={waitTimeout} onChange={(event) => setWaitTimeout(Number(event.target.value))} /></Field>
                      </div>
                      {!isAndroid && <div className="grid grid-cols-2 gap-3">
                        <Field><FieldLabel htmlFor="wait-selector">Wait selector</FieldLabel><Input id="wait-selector" value={waitSelector} onChange={(event) => setWaitSelector(event.target.value)} placeholder=".ready" /></Field>
                        <Field><FieldLabel htmlFor="wait-text">Wait text</FieldLabel><Input id="wait-text" value={waitText} onChange={(event) => setWaitText(event.target.value)} placeholder="Loaded" /></Field>
                      </div>}
                      {["jpeg", "webp", "avif"].includes(output) && <Field><FieldLabel htmlFor="quality">Image quality</FieldLabel><Input id="quality" type="number" min={1} max={100} value={quality} onChange={(event) => setQuality(Number(event.target.value))} /></Field>}
                      {["png", "webp", "avif"].includes(output) && <Field orientation="horizontal"><FieldLabel htmlFor="transparent">Transparent background</FieldLabel><Switch id="transparent" checked={transparent} onCheckedChange={setTransparent} /></Field>}
                      {!isAndroid && engine === "chromium" && (output === "png" || output === "webp") && <Field orientation="horizontal"><FieldLabel htmlFor="optimize-image"><span><span className="block">Fast {output.toUpperCase()} encoding</span><FieldDescription>Prioritizes render speed over the smallest file size.</FieldDescription></span></FieldLabel><Switch id="optimize-image" checked={optimizePng} onCheckedChange={setOptimizePng} /></Field>}
                      {!isAndroid && <Field><FieldLabel htmlFor="headers">Same-origin headers</FieldLabel><Input id="headers" value={headers} onChange={(event) => setHeaders(event.target.value)} placeholder={'{"Authorization":"Bearer …"}'} /><FieldDescription>Sent only to the exact target origin.</FieldDescription></Field>}
                      {!isAndroid && <Field><FieldLabel htmlFor="expert-overrides">Expert JSON overrides</FieldLabel><InputGroup><InputGroupTextarea id="expert-overrides" rows={7} value={expertOverrides} onChange={(event) => setExpertOverrides(event.target.value)} spellCheck={false} /></InputGroup><FieldDescription>Deep-merges into the generated request. Use for actions, assertions, cookies, proxies, profiles, viewport packs, certification, and other strict API fields.</FieldDescription></Field>}
                    </FieldGroup>
                  </CollapsibleContent>
                </Collapsible>
              </FieldGroup>
            </aside>

            <section className="flex min-w-0 flex-col bg-card p-4 sm:p-6">
              <div className="mb-4 flex items-center justify-between">
                <div><p className="font-medium">Result</p><p className="text-xs text-muted-foreground">Preview and download your latest capture.</p></div>
                <Badge variant={status === "Complete" ? "default" : "secondary"}>{status}</Badge>
              </div>
              <div className="subtle-grid flex min-h-[420px] flex-1 items-center justify-center overflow-hidden rounded-xl border bg-muted/20 p-4">
                {busy ? (
                  <div className="text-center"><Loader2 className="mx-auto size-8 animate-spin text-primary" /><p className="mt-3 text-sm text-muted-foreground">Loading and rendering…</p></div>
                ) : latest?.text ? (
                  <pre className="max-h-[580px] w-full overflow-auto whitespace-pre-wrap rounded-lg border bg-background p-4 text-xs">{latest.text}</pre>
                ) : latest?.type.startsWith("video/") ? (
                  <video src={latest.url} controls className="max-h-[580px] max-w-full rounded-lg border bg-background" />
                ) : latest?.type === "application/pdf" ? (
                  <iframe src={latest.url} title="Latest ViperCapture PDF" className="h-[580px] w-full rounded-lg border bg-background" />
                ) : latest?.type.startsWith("image/") ? (
                  <img src={latest.url} alt="Latest ViperCapture result" className="max-h-[580px] max-w-full rounded-lg border bg-background object-contain shadow-xl" />
                ) : latest ? (
                  <div className="max-w-sm text-center"><p className="font-medium">Artifact ready</p><p className="mt-1 text-sm text-muted-foreground">Preview is unavailable for {latest.type || "this format"}. Download the result below.</p></div>
                ) : (
                  <div className="max-w-sm text-center"><div className="mx-auto flex size-12 items-center justify-center rounded-xl border bg-background"><ImageIcon className="size-5 text-muted-foreground" /></div><p className="mt-4 font-medium">Your capture will appear here</p><p className="mt-1 text-sm leading-6 text-muted-foreground">Choose a URL and capture settings, then run the renderer.</p></div>
                )}
              </div>
              {latest && (
                <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0"><p className="truncate text-sm font-medium">{latest.name}</p><p className="text-xs text-muted-foreground">{latest.type}</p></div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => void openDownloads()}><FolderOpen data-icon="inline-start" />Downloads</Button>
                    <Button size="sm" onClick={() => void downloadCapture(latest)}><Download data-icon="inline-start" />Download</Button>
                  </div>
                </div>
              )}
              {history.length > 1 && (
                <div className="mt-5 border-t pt-4">
                  <p className="mono-label mb-3">Recent in this tab</p>
                  <div className="flex gap-2 overflow-x-auto pb-1">
                    {history.map((item) => (
                      <button key={item.url} onClick={() => { setLatest(item); if (isAndroid) void downloadCapture(item) }} className="flex min-w-36 items-center gap-2 rounded-lg border bg-background p-2 text-xs hover:bg-accent">
                        <CheckCircle2 className="size-4 text-primary" /><span className="truncate">{item.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </section>
          </div>
        </section>

        <footer className="flex flex-col gap-3 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>Open source under the MIT License.</p>
          <Button variant="link" size="sm" onClick={() => void openExternal("cloud")}>Try ViperCapture Cloud <ExternalLink data-icon="inline-end" /></Button>
        </footer>
      </main>

      <AlertDialog open={captchaWarning !== null} onOpenChange={(open: boolean) => { if (!open) setCaptchaWarning(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertTriangle />
            <AlertDialogTitle>CAPTCHA detected</AlertDialogTitle>
            <AlertDialogDescription>
              {captchaWarning?.provider} is blocking the page. You can capture the visible challenge, but ViperCapture will not solve or bypass it.
              {captchaWarning?.requestId ? ` Request ${captchaWarning.requestId}.` : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel capture</AlertDialogCancel>
            <AlertDialogAction onClick={() => { setCaptchaWarning(null); void capture(true) }}>Capture anyway</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
