import { useEffect, useMemo, useRef, useState } from "react"
import {
  AlertTriangle,
  Bookmark,
  CheckCircle2,
  ChevronDown,
  Download,
  ExternalLink,
  Code2,
  Globe2,
  ImageIcon,
  Loader2,
  Moon,
  RotateCcw,
  Square,
  Sun,
  Trash2,
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
import { BookTextIcon } from "@/components/ui/book-text"
import { Button } from "@/components/ui/button"
import { ClapIcon } from "@/components/ui/clap"
import { ContrastIcon } from "@/components/ui/contrast"
import { CpuIcon } from "@/components/ui/cpu"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { EyeIcon } from "@/components/ui/eye"
import { FileTextIcon } from "@/components/ui/file-text"
import { FrameIcon } from "@/components/ui/frame"
import { GalleryThumbnailsIcon } from "@/components/ui/gallery-thumbnails"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupTextarea } from "@/components/ui/input-group"
import { MonitorCheckIcon } from "@/components/ui/monitor-check"
import { MonitorCogIcon } from "@/components/ui/monitor-cog"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { SettingsIcon } from "@/components/ui/settings"
import { ShieldCheckIcon } from "@/components/ui/shield-check"
import { SlidersHorizontalIcon } from "@/components/ui/sliders-horizontal"
import { Switch } from "@/components/ui/switch"
import { TimerIcon } from "@/components/ui/timer"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

type Output = "png" | "jpeg" | "webp" | "avif" | "pdf" | "html" | "markdown" | "metadata" | "webm" | "mp4" | "gif"
type Device = "desktop" | "iphone_14" | "pixel_7" | "ipad"
type Capture = {
  name: string
  url: string
  type: string
  width?: number
  height?: number
  sizeBytes: number
  renderMs?: number
  requestId?: string
}
type ActiveRun = { waitConditions: string; waitTimeout: number }
type AppConfig = {
  max_screenshot_pixels?: number
  max_viewport_width?: number
  max_viewport_height?: number
  max_full_page_height?: number
  control_plane?: boolean
  presets?: boolean
  gpu?: { mode?: "off" | "auto" | "required"; hardware_active?: boolean; mutable?: boolean }
}
type CaptchaWarning = { provider: string; requestId?: string }

type PresetSettings = {
  output: Output
  width: number
  height: number
  density: number
  fullPage: boolean
  preserveViewportWidth: boolean
  selector: string
  quality: number
  transparent: boolean
  lazyLoad: string
  optimizePng: boolean
  diagnostics: boolean
  videoDuration: number
  videoFps: number
  videoBitrate: number
  videoScroll: boolean
  waitEvent: string
  waitDelay: number
  waitSelector: string
  waitText: string
  waitTimeout: number
  headers: string
  consent: string
  blockAds: boolean
  blockTrackers: boolean
  blockChats: boolean
  blockNewsletters: boolean
  device: Device
  colorScheme: string
  reducedMotion: string
  locale: string
  timezone: string
  customCss: string
  failStatuses: string
  clipEnabled: boolean
  clipX: number
  clipY: number
  clipWidth: number
  clipHeight: number
  extractMode: string
  pdfMode: string
  paperSize: string
  orientation: string
  pdfMargin: number
}
type SavedPreset = { name: string; settings: PresetSettings }

const defaultSettings: PresetSettings = {
  output: "png",
  width: 1280,
  height: 720,
  density: 1,
  fullPage: true,
  preserveViewportWidth: false,
  selector: "",
  quality: 90,
  transparent: false,
  lazyLoad: "adaptive",
  optimizePng: true,
  diagnostics: false,
  videoDuration: 5,
  videoFps: 60,
  videoBitrate: 20,
  videoScroll: false,
  waitEvent: "load",
  waitDelay: 0,
  waitSelector: "",
  waitText: "",
  waitTimeout: 15,
  headers: "",
  consent: "reject",
  blockAds: true,
  blockTrackers: true,
  blockChats: true,
  blockNewsletters: true,
  device: "desktop",
  colorScheme: "system",
  reducedMotion: "system",
  locale: "",
  timezone: "",
  customCss: "",
  failStatuses: "",
  clipEnabled: false,
  clipX: 0,
  clipY: 0,
  clipWidth: 640,
  clipHeight: 480,
  extractMode: "document",
  pdfMode: "print",
  paperSize: "A4",
  orientation: "portrait",
  pdfMargin: 0.4,
}

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
const docsUrl = "https://github.com/Viperisuseful/ViperCapture/blob/master/docs/self-hosting.md"
const siteAccessUrl = "https://github.com/Viperisuseful/ViperCapture/blob/master/docs/site-access.md"
const blockedHeaderNames = new Set([
  "connection", "content-length", "forwarded", "host", "keep-alive",
  "proxy-authenticate", "proxy-authorization", "te", "trailer",
  "transfer-encoding", "upgrade",
])
const blockedHeaderPrefixes = ["proxy-", "sec-", "x-forwarded-"]
const headerNamePattern = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

function extension(output: Output) {
  if (output === "jpeg") return "jpg"
  if (output === "markdown") return "md"
  if (output === "metadata") return "json"
  return output
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  const units = ["KiB", "MiB", "GiB"]
  let value = bytes / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024
    unit = units[index]
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`
}

function formatDuration(milliseconds: number) {
  return milliseconds < 1000
    ? `${milliseconds} ms`
    : `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`
}

function integerHeader(response: Response, name: string) {
  const value = Number(response.headers.get(name))
  return Number.isFinite(value) && value >= 0 ? Math.round(value) : undefined
}

function validateSelector(value: string) {
  const selector = value.trim()
  if (!selector) return null
  if (selector.length > 2048) return "Selectors may contain at most 2,048 characters."
  if (/::[a-z-]+/i.test(selector)) {
    return "Pseudo-elements are unsupported. Select the element itself."
  }
  try {
    document.createDocumentFragment().querySelector(selector)
    return null
  } catch {
    return "Enter a valid CSS selector, such as main, #invoice, or .ready."
  }
}

function parseCustomHeaders(value: string): {
  headers: Record<string, string>
  error: string | null
} {
  if (!value.trim()) return { headers: {}, error: null }
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    return { headers: {}, error: 'Enter valid JSON, for example {"Accept-Language":"en-US"}.' }
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    return { headers: {}, error: "Custom headers must be a JSON object." }
  }
  const entries = Object.entries(parsed)
  if (entries.length > 32) return { headers: {}, error: "Custom headers may contain at most 32 entries." }
  const seenNames = new Set<string>()
  let totalBytes = 0
  for (const [name, headerValue] of entries) {
    const lowered = name.toLowerCase()
    if (!headerNamePattern.test(name) || new TextEncoder().encode(name).length > 128) {
      return { headers: {}, error: `“${name}” is not a valid HTTP header name.` }
    }
    if (blockedHeaderNames.has(lowered) || blockedHeaderPrefixes.some((prefix) => lowered.startsWith(prefix))) {
      return { headers: {}, error: `“${name}” is managed by ViperCapture and cannot be overridden.` }
    }
    if (seenNames.has(lowered)) {
      return { headers: {}, error: `“${name}” duplicates another header name with different capitalization.` }
    }
    seenNames.add(lowered)
    if (typeof headerValue !== "string") return { headers: {}, error: `The value for “${name}” must be text.` }
    if (
      new TextEncoder().encode(headerValue).length > 4096 ||
      [...headerValue].some((character) => {
        const code = character.charCodeAt(0)
        return code < 32 || code === 127
      })
    ) {
      return { headers: {}, error: `The value for “${name}” is too long or contains control characters.` }
    }
    totalBytes += new TextEncoder().encode(name).length + new TextEncoder().encode(headerValue).length + 4
  }
  if (totalBytes > 16 * 1024) return { headers: {}, error: "Custom headers may use at most 16 KiB." }
  return { headers: Object.fromEntries(entries) as Record<string, string>, error: null }
}

function ResultMetric({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={mono ? "mt-1 truncate font-mono text-xs" : "mt-1 text-sm font-medium"} title={value}>{value}</dd>
    </div>
  )
}

export default function App() {
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem("theme")
    return stored === null
      ? window.matchMedia("(prefers-color-scheme: dark)").matches
      : stored === "dark"
  })
  const [url, setUrl] = useState("https://example.com")
  const [apiKey, setApiKey] = useState("")
  const [output, setOutput] = useState<Output>("png")
  const [width, setWidth] = useState(1280)
  const [height, setHeight] = useState(720)
  const [density, setDensity] = useState(1)
  const [fullPage, setFullPage] = useState(true)
  const [preserveViewportWidth, setPreserveViewportWidth] = useState(false)
  const [selector, setSelector] = useState("")
  const [quality, setQuality] = useState(90)
  const [transparent, setTransparent] = useState(false)
  const [lazyLoad, setLazyLoad] = useState("adaptive")
  const [optimizePng, setOptimizePng] = useState(true)
  const [diagnostics, setDiagnostics] = useState(false)
  const [videoDuration, setVideoDuration] = useState(5)
  const [videoFps, setVideoFps] = useState(60)
  const [videoBitrate, setVideoBitrate] = useState(20)
  const [videoScroll, setVideoScroll] = useState(false)
  const [waitEvent, setWaitEvent] = useState("load")
  const [waitDelay, setWaitDelay] = useState(0)
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
  const [pdfMode, setPdfMode] = useState("print")
  const [paperSize, setPaperSize] = useState("A4")
  const [orientation, setOrientation] = useState("portrait")
  const [pdfMargin, setPdfMargin] = useState(0.4)
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [gpuBusy, setGpuBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState("Ready")
  const [elapsedMs, setElapsedMs] = useState(0)
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null)
  const [headersTouched, setHeadersTouched] = useState(false)
  const [selectorTouched, setSelectorTouched] = useState(false)
  const [waitSelectorTouched, setWaitSelectorTouched] = useState(false)
  const [latest, setLatest] = useState<Capture | null>(null)
  const [history, setHistory] = useState<Capture[]>([])
  const [captchaWarning, setCaptchaWarning] = useState<CaptchaWarning | null>(null)
  const [savedPresets, setSavedPresets] = useState<SavedPreset[]>([])
  const [presetName, setPresetName] = useState("")
  const [savePresetOpen, setSavePresetOpen] = useState(false)
  const historyRef = useRef<Capture[]>([])
  const abortControllerRef = useRef<AbortController | null>(null)
  const captureStartedAtRef = useRef(0)

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
    localStorage.setItem("theme", dark ? "dark" : "light")
  }, [dark])
  useEffect(() => {
    fetch("/app-config").then((response) => response.json()).then(setConfig).catch(() => null)
    fetch("/local/presets")
      .then((response) => (response.ok ? response.json() : { presets: [] }))
      .then((body) => setSavedPresets(Array.isArray(body.presets) ? body.presets : []))
      .catch(() => null)
  }, [])
  useEffect(
    () => () => {
      abortControllerRef.current?.abort()
      historyRef.current.forEach((item) => URL.revokeObjectURL(item.url))
    },
    [],
  )
  useEffect(() => {
    if (!busy) return
    const updateElapsed = () => setElapsedMs(performance.now() - captureStartedAtRef.current)
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 250)
    return () => window.clearInterval(timer)
  }, [busy])

  const maxPixels = config?.max_screenshot_pixels ?? 500_000_000
  const maxWidth = config?.max_viewport_width ?? 16_384
  const maxHeight = config?.max_viewport_height ?? 16_384
  const imageOutput = output === "png" || output === "jpeg" || output === "webp" || output === "avif"
  const videoOutput = output === "webm" || output === "mp4" || output === "gif"
  const gpuEnabled = config?.gpu?.mode !== "off"
  const gpuCopy = useMemo(() => {
    if (!gpuEnabled) return "Uses Chromium's default software-compatible mode."
    return config?.gpu?.hardware_active
      ? "Hardware compositing verified by Chromium."
      : "GPU requested; hardware acceleration was not verified."
  }, [config, gpuEnabled])

  const fits = (w: number, h: number, scale = density) =>
    w * h * scale * scale <= maxPixels
  const headersValidation = useMemo(() => parseCustomHeaders(headers), [headers])
  const selectorError = useMemo(() => validateSelector(selector), [selector])
  const waitSelectorError = useMemo(() => validateSelector(waitSelector), [waitSelector])
  const waitConditions = useMemo(() => {
    const conditions = [
      waitEvent === "domcontentloaded" ? "DOM readiness" : waitEvent === "networkidle" ? "network quiet" : "page load",
      waitSelector.trim() ? `visible ${waitSelector.trim()}` : null,
      waitText.trim() ? `text “${waitText.trim().slice(0, 48)}”` : null,
      waitDelay > 0 ? `${waitDelay}s settle delay` : null,
      fullPage && lazyLoad !== "none" ? `${lazyLoad} lazy-content scan` : null,
    ].filter(Boolean)
    return conditions.join(" → ")
  }, [fullPage, lazyLoad, waitDelay, waitEvent, waitSelector, waitText])
  const progressStage = useMemo(() => {
    const elapsed = elapsedMs / 1000
    const waitWindow = Math.min(Math.max(activeRun?.waitTimeout ?? 15, 5), 15)
    if (elapsed < 1.5) return "Securing a render slot"
    if (elapsed < 3.5) return "Opening the page"
    if (elapsed < 3.5 + waitWindow) return `Waiting for ${activeRun?.waitConditions ?? "page readiness"}`
    if (elapsed < 7 + waitWindow) return "Capturing the page"
    if (elapsed < 10 + waitWindow) return "Finalizing the image"
    return "Still working — this page needs a little longer"
  }, [activeRun, elapsedMs])
  const progressValue = Math.min(
    92,
    6 + (elapsedMs / Math.max(10_000, ((activeRun?.waitTimeout ?? 15) + 10) * 1000)) * 86,
  )

  const currentSettings = (): PresetSettings => ({
    output, width, height, density, fullPage, preserveViewportWidth, selector,
    quality, transparent, lazyLoad, optimizePng, diagnostics, videoDuration,
    videoFps, videoBitrate, videoScroll, waitEvent, waitDelay, waitSelector,
    waitText, waitTimeout, headers, consent, blockAds, blockTrackers, blockChats,
    blockNewsletters, device, colorScheme, reducedMotion, locale, timezone,
    customCss, failStatuses, clipEnabled, clipX, clipY, clipWidth, clipHeight,
    extractMode, pdfMode, paperSize, orientation, pdfMargin,
  })

  const applySettings = (value: Partial<PresetSettings>) => {
    const merged = { ...defaultSettings, ...value }
    const imageOutput = ["png", "jpeg", "webp", "avif"].includes(merged.output)
    setOutput(merged.output)
    setWidth(merged.width)
    setHeight(merged.height)
    setDensity(merged.density)
    setFullPage(merged.fullPage)
    setPreserveViewportWidth(merged.preserveViewportWidth)
    setSelector(merged.selector)
    setQuality(merged.quality)
    setTransparent(merged.transparent)
    setLazyLoad(merged.lazyLoad)
    setOptimizePng(merged.optimizePng)
    setDiagnostics(merged.diagnostics)
    setVideoDuration(merged.videoDuration)
    setVideoFps(merged.videoFps)
    setVideoBitrate(merged.videoBitrate)
    setVideoScroll(merged.videoScroll)
    setWaitEvent(merged.waitEvent)
    setWaitDelay(merged.waitDelay)
    setWaitSelector(merged.waitSelector)
    setWaitText(merged.waitText)
    setWaitTimeout(merged.waitTimeout)
    setHeaders(merged.headers)
    setConsent(merged.consent)
    setBlockAds(merged.blockAds)
    setBlockTrackers(merged.blockTrackers)
    setBlockChats(merged.blockChats)
    setBlockNewsletters(merged.blockNewsletters)
    setDevice(merged.device)
    setColorScheme(merged.colorScheme)
    setReducedMotion(merged.reducedMotion)
    setLocale(merged.locale)
    setTimezone(merged.timezone)
    setCustomCss(merged.customCss)
    setFailStatuses(merged.failStatuses)
    setClipEnabled(merged.clipEnabled && imageOutput)
    setClipX(merged.clipX)
    setClipY(merged.clipY)
    setClipWidth(merged.clipWidth)
    setClipHeight(merged.clipHeight)
    setExtractMode(merged.extractMode)
    setPdfMode(merged.pdfMode)
    setPaperSize(merged.paperSize)
    setOrientation(merged.orientation)
    setPdfMargin(merged.pdfMargin)
    setHeadersTouched(false)
    setSelectorTouched(false)
    setWaitSelectorTouched(false)
  }

  const reset = () => applySettings(defaultSettings)

  // Settings are flat; sorting keys makes the comparison order-insensitive.
  const stableStringify = (value: PresetSettings) =>
    JSON.stringify(value, Object.keys(value).sort())
  const currentSettingsJson = stableStringify(currentSettings())
  const matchedPreset = savedPresets.find(
    (preset) => stableStringify(preset.settings) === currentSettingsJson,
  )

  const savePreset = async () => {
    const name = presetName.trim().slice(0, 40)
    if (!name) {
      toast.error("Give this preset a name.")
      return
    }
    if (savedPresets.some((preset) => preset.name === name)) {
      toast.error("A preset with that name already exists.")
      return
    }
    try {
      const response = await fetch("/local/presets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, settings: currentSettings() }),
      })
      if (response.status === 409) {
        toast.error("A preset with that name already exists.")
        return
      }
      if (!response.ok) {
        const body = await response.json().catch(() => null)
        toast.error(body?.error?.message ?? "Could not save the preset.")
        return
      }
      const body = await response.json()
      setSavedPresets(body.presets)
      setPresetName("")
      setSavePresetOpen(false)
      toast.success("Preset saved")
    } catch {
      toast.error("Could not reach the preset store.")
    }
  }

  const deletePreset = async (name: string) => {
    try {
      const response = await fetch(`/local/presets/${encodeURIComponent(name)}`, { method: "DELETE" })
      if (!response.ok) {
        toast.error("Could not delete the preset.")
        return
      }
      const body = await response.json()
      setSavedPresets(body.presets)
    } catch {
      toast.error("Could not reach the preset store.")
    }
  }

  const setGpu = async (enabled: boolean) => {
    if (!config?.gpu?.mutable || gpuBusy) return
    setGpuBusy(true)
    try {
      const response = await fetch("/local/gpu-mode", {
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
    if (!url.trim()) return toast.error("Enter a public website URL.")
    if (!fits(width, height)) return toast.error("The selected viewport and density exceed the server pixel limit.")
    if (selectorError || waitSelectorError || headersValidation.error) {
      setSelectorTouched(true)
      setWaitSelectorTouched(true)
      setHeadersTouched(true)
      return toast.error("Fix the highlighted advanced settings before capturing.")
    }
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
    const customHeaders = headersValidation.headers
    const normalized = /^https?:\/\//i.test(url.trim()) ? url.trim() : `https://${url.trim()}`
    const payload = {
      url: normalized,
      output,
      viewport: { width, height, device_scale_factor: density },
      environment: {
        device,
        color_scheme: colorScheme === "system" ? null : colorScheme,
        reduced_motion: reducedMotion === "system" ? null : reducedMotion,
        locale: locale.trim() || null,
        timezone: timezone.trim() || null,
      },
      full_page: imageOutput && (selector || clipEnabled) ? false : fullPage,
      preserve_viewport_width: imageOutput && !selector && !clipEnabled && fullPage && preserveViewportWidth,
      lazy_load: lazyLoad,
      selector: imageOutput && !clipEnabled ? selector || null : null,
      clip: imageOutput && clipEnabled
        ? { x: clipX, y: clipY, width: clipWidth, height: clipHeight }
        : null,
      custom_css: customCss || null,
      fail_on_status: parsedStatuses,
      image: {
        quality: output === "jpeg" || output === "webp" || output === "avif" ? quality : null,
        transparent_background: (output === "png" || output === "webp" || output === "avif") && transparent,
        optimize_for_speed: (output === "png" || output === "webp") && optimizePng,
      },
      diagnostics: { bundle: diagnostics },
      video: videoOutput ? {
        duration_ms: videoDuration * 1000,
        fps: videoFps,
        bitrate_mbps: videoBitrate,
        scroll: !fullPage && videoScroll,
        transparent_background: fullPage && output !== "mp4" && transparent,
      } : null,
      pdf: output === "pdf" ? {
        mode: pdfMode,
        paper_size: paperSize,
        orientation,
        print_background: true,
        margins: { top: pdfMargin, right: pdfMargin, bottom: pdfMargin, left: pdfMargin },
      } : null,
      extract_mode: output === "html" || output === "markdown" ? extractMode : "document",
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
    const controller = new AbortController()
    abortControllerRef.current = controller
    captureStartedAtRef.current = performance.now()
    setActiveRun({ waitConditions, waitTimeout })
    setElapsedMs(0)
    setBusy(true)
    setStatus("Rendering")
    try {
      const response = await fetch("/v1/render", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })
      const requestId = response.headers.get("x-request-id") ?? undefined
      const renderMs = integerHeader(response, "x-vipercapture-render-ms")
      const responseWidth = integerHeader(response, "x-vipercapture-width")
      const responseHeight = integerHeader(response, "x-vipercapture-height")
      if (!response.ok) {
        const body = await response.json().catch(() => null)
        const failureRequestId = body?.error?.request_id ?? requestId
        if (response.status === 409 && body?.error?.code === "captcha_detected") {
          const provider = body?.error?.details?.provider ?? "unknown"
          setCaptchaWarning({ provider: providerNames[provider] ?? provider, requestId: failureRequestId })
          setStatus("Challenge detected")
          return
        }
        const message = body?.detail ?? body?.error?.message ?? `Capture failed (${response.status})`
        throw new Error(failureRequestId ? `${message} · Request ${failureRequestId}` : message)
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
      const item = {
        name: serverName ?? `${host}_${Date.now()}.${extension(output)}`,
        url: URL.createObjectURL(blob),
        type: blob.type,
        width: responseWidth,
        height: responseHeight,
        sizeBytes: blob.size,
        renderMs,
        requestId,
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
      if (error instanceof DOMException && error.name === "AbortError") {
        setStatus("Cancelled")
        toast("Capture cancelled")
      } else {
        setStatus("Failed")
        toast.error(error instanceof Error ? error.message : "Capture failed")
      }
    } finally {
      if (abortControllerRef.current === controller) abortControllerRef.current = null
      setBusy(false)
    }
  }

  const cancelCapture = () => abortControllerRef.current?.abort()

  return (
    <div className="min-h-svh bg-background text-foreground">
      <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
        <div className="site-shell flex h-16 items-center justify-between">
          <a href="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <img src="/static/vipercapture-mark.svg" alt="" className="size-8 rounded-lg" />
            ViperCapture
            <Badge variant="secondary" className="hidden sm:inline-flex">Open source</Badge>
          </a>
          <nav className="flex items-center gap-1">
            <Button variant="ghost" size="icon-sm" onClick={() => setDark((value) => !value)} aria-label="Toggle color theme">
              {dark ? <Sun /> : <Moon />}
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a href="https://github.com/Viperisuseful/ViperCapture" target="_blank" rel="noreferrer">
                <Code2 data-icon="inline-start" />
                <span className="hidden sm:inline">GitHub</span>
              </a>
            </Button>
          </nav>
        </div>
      </header>

      <main className="site-shell py-10 sm:py-14">
        <h1 className="sr-only">ViperCapture</h1>
        <section className="hairline-panel overflow-hidden">
          <div className="flex flex-col gap-4 border-b bg-card p-4 lg:flex-row lg:items-end">
            <Field className="min-w-0 flex-1">
              <FieldLabel htmlFor="capture-url">Website URL</FieldLabel>
              <InputGroup className="h-auto min-h-10">
                <InputGroupAddon><Globe2 /></InputGroupAddon>
                <InputGroupTextarea id="capture-url" rows={1} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com" className="min-h-10" />
              </InputGroup>
            </Field>
            {config?.control_plane && (
              <Field className="lg:w-72">
                <FieldLabel htmlFor="project-api-key">Project API key</FieldLabel>
                <Input id="project-api-key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="vcp_…" />
                <FieldDescription>Kept only in memory until this page closes.</FieldDescription>
              </Field>
            )}
            <Button size="lg" onClick={() => void capture()} disabled={busy} className="lg:min-w-36">
              {busy ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <ImageIcon data-icon="inline-start" />}
              {busy ? "Rendering" : "Capture"}
            </Button>
          </div>

          <div className="grid min-h-[650px] lg:grid-cols-[380px_minmax(0,1fr)]">
            <aside className="border-b bg-muted/25 p-4 lg:border-r lg:border-b-0">
              <div className="mb-5 flex items-center justify-between">
                <div className="flex items-start gap-2.5">
                  <SettingsIcon data-title-icon="capture-settings" size={18} className="mt-0.5 shrink-0 text-primary" aria-hidden="true" />
                  <div><p className="font-medium">Capture settings</p><p className="text-xs text-muted-foreground">Runs on your machine</p></div>
                </div>
                <div className="flex items-center gap-1">
                  {config?.presets && (
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      onClick={() => { setPresetName(matchedPreset?.name ?? ""); setSavePresetOpen(true) }}
                      aria-label={matchedPreset ? `Manage presets (${matchedPreset.name} applied)` : "Save preset"}
                      title={matchedPreset ? `${matchedPreset.name} applied` : "Save preset"}
                    >
                      <Bookmark fill={matchedPreset ? "currentColor" : "none"} />
                    </Button>
                  )}
                  <Button size="icon-sm" variant="ghost" onClick={reset} aria-label="Reset settings"><RotateCcw /></Button>
                </div>
              </div>
              {config?.presets && savedPresets.length > 0 && (
                <Select
                  value={matchedPreset?.name ?? ""}
                  onValueChange={(name: string) => {
                    const preset = savedPresets.find((item) => item.name === name)
                    if (preset) {
                      applySettings(preset.settings)
                      toast.success(`Applied ${preset.name}`)
                    }
                  }}
                >
                  <SelectTrigger className="mb-5 w-full" aria-label="Load saved preset">
                    <SelectValue placeholder="Load preset" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {savedPresets.map((preset) => (
                        <SelectItem key={preset.name} value={preset.name}>{preset.name}</SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              )}
              <FieldGroup>
                <FieldSet>
                  <FieldLegend className="flex items-center gap-2"><GalleryThumbnailsIcon data-title-icon="output" size={16} className="shrink-0 text-primary" aria-hidden="true" />Output</FieldLegend>
                  <Field>
                    <Select value={output} onValueChange={(value: string) => setOutput(value as Output)}>
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectGroup>
                        <SelectItem value="png">PNG image</SelectItem><SelectItem value="jpeg">JPEG image</SelectItem><SelectItem value="webp">WebP image</SelectItem><SelectItem value="avif">AVIF image</SelectItem>
                        <SelectItem value="pdf">PDF document</SelectItem><SelectItem value="html">Hydrated HTML</SelectItem><SelectItem value="markdown">Markdown</SelectItem>
                        <SelectItem value="metadata">Metadata JSON</SelectItem><SelectItem value="webm">WebM video</SelectItem><SelectItem value="mp4">MP4 video</SelectItem><SelectItem value="gif">Animated GIF</SelectItem>
                      </SelectGroup></SelectContent>
                    </Select>
                  </Field>
                </FieldSet>

                <FieldSet>
                  <FieldLegend className="flex items-center gap-2"><MonitorCheckIcon data-title-icon="viewport" size={16} className="shrink-0 text-primary" aria-hidden="true" />Viewport</FieldLegend>
                  <div className="grid grid-cols-3 gap-3">
                    <Field><FieldLabel htmlFor="width">Width</FieldLabel><Input id="width" type="number" min={1} max={maxWidth} value={width} onChange={(event) => setWidth(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="height">Height</FieldLabel><Input id="height" type="number" min={1} max={maxHeight} value={height} onChange={(event) => setHeight(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="density">Density</FieldLabel><Input id="density" type="number" min={0.1} max={8} step={0.25} value={density} onChange={(event) => setDensity(Number(event.target.value))} /></Field>
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
                  <Field orientation="horizontal">
                    <FieldLabel htmlFor="full-page"><span><span className="block">Full page</span><FieldDescription>Scroll and capture the document.</FieldDescription></span></FieldLabel>
                    <Switch id="full-page" checked={fullPage} onCheckedChange={(checked: boolean) => setFullPage(checked)} disabled={Boolean(selector) || clipEnabled} />
                  </Field>
                  {imageOutput && fullPage && !selector && !clipEnabled && (
                    <Field orientation="horizontal">
                      <FieldLabel htmlFor="preserve-width"><span><span className="block">Preserve viewport width</span><FieldDescription>Clip horizontal overflow while keeping the full page height.</FieldDescription></span></FieldLabel>
                      <Switch id="preserve-width" checked={preserveViewportWidth} onCheckedChange={setPreserveViewportWidth} />
                    </Field>
                  )}
                </FieldSet>

                <FieldSet>
                  <FieldLegend className="flex items-center gap-2"><CpuIcon data-title-icon="rendering" size={16} className="shrink-0 text-primary" aria-hidden="true" />Rendering</FieldLegend>
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

                <FieldSet>
                  <FieldLegend className="flex items-center gap-2"><ShieldCheckIcon data-title-icon="page-cleanup" size={16} className="shrink-0 text-primary" aria-hidden="true" />Page cleanup</FieldLegend>
                  <Field>
                    <FieldLabel>Cookie consent</FieldLabel>
                    <Select value={consent} onValueChange={setConsent}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
                      <SelectItem value="reject">Reject non-essential</SelectItem><SelectItem value="accept">Accept</SelectItem><SelectItem value="hide">Hide banner</SelectItem><SelectItem value="none">Leave unchanged</SelectItem>
                    </SelectGroup></SelectContent></Select>
                  </Field>
                  {[
                    ["cleanup-ads", "Ads", blockAds, setBlockAds],
                    ["cleanup-trackers", "Trackers", blockTrackers, setBlockTrackers],
                    ["cleanup-chats", "Chat widgets", blockChats, setBlockChats],
                    ["cleanup-newsletters", "Newsletters", blockNewsletters, setBlockNewsletters],
                  ].map(([id, label, checked, setter]) => (
                    <Field key={String(id)} orientation="horizontal">
                      <FieldLabel htmlFor={String(id)}>{String(label)}</FieldLabel>
                      <Switch id={String(id)} checked={checked as boolean} onCheckedChange={setter as (value: boolean) => void} />
                    </Field>
                  ))}
                </FieldSet>

                <Collapsible>
                  <CollapsibleTrigger asChild>
                    <Button variant="outline" className="w-full justify-between"><span className="flex items-center gap-2"><SlidersHorizontalIcon data-title-icon="advanced-controls" size={16} className="shrink-0 text-primary" aria-hidden="true" />Advanced controls</span><ChevronDown /></Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-4">
                    <FieldGroup>
                      <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><MonitorCogIcon data-title-icon="deterministic-environment" size={16} className="shrink-0 text-primary" aria-hidden="true" />Deterministic environment</FieldLegend>
                        <Field><FieldLabel>Device signals</FieldLabel><Select value={device} onValueChange={(value) => setDevice(value as Device)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
                          <SelectItem value="desktop">Desktop Chromium</SelectItem><SelectItem value="iphone_14">iPhone 14</SelectItem><SelectItem value="pixel_7">Pixel 7</SelectItem><SelectItem value="ipad">iPad</SelectItem>
                        </SelectGroup></SelectContent></Select><FieldDescription>Applies user-agent, touch, and mobile signals without changing the explicit viewport.</FieldDescription></Field>
                        <div className="grid grid-cols-2 gap-3">
                          <Field><FieldLabel>Color scheme</FieldLabel><Select value={colorScheme} onValueChange={setColorScheme}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="system">Browser default</SelectItem><SelectItem value="light">Light</SelectItem><SelectItem value="dark">Dark</SelectItem><SelectItem value="no-preference">No preference</SelectItem></SelectGroup></SelectContent></Select></Field>
                          <Field><FieldLabel>Reduced motion</FieldLabel><Select value={reducedMotion} onValueChange={setReducedMotion}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="system">Browser default</SelectItem><SelectItem value="reduce">Reduce</SelectItem><SelectItem value="no-preference">No preference</SelectItem></SelectGroup></SelectContent></Select></Field>
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                          <Field><FieldLabel htmlFor="locale">Locale</FieldLabel><Input id="locale" value={locale} onChange={(event) => setLocale(event.target.value)} placeholder="en-US" /></Field>
                          <Field><FieldLabel htmlFor="timezone">IANA timezone</FieldLabel><Input id="timezone" value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="America/New_York" /></Field>
                        </div>
                      </FieldSet>
                      <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><FrameIcon data-title-icon="capture-region" size={16} className="shrink-0 text-primary" aria-hidden="true" />Capture region</FieldLegend>
                        <Field data-invalid={selectorTouched && Boolean(selectorError)}>
                          <FieldLabel htmlFor="selector">Element selector · <a href={`${docsUrl}#selectors-and-waits`} target="_blank" rel="noreferrer">Docs</a></FieldLabel>
                          <Input id="selector" value={selector} onChange={(event) => { setSelector(event.target.value); if (event.target.value) setClipEnabled(false) }} onBlur={() => setSelectorTouched(true)} placeholder="main, #invoice" aria-invalid={selectorTouched && Boolean(selectorError)} />
                          {selectorTouched && <FieldError>{selectorError}</FieldError>}
                        </Field>
                        <Field orientation="horizontal"><FieldLabel htmlFor="clip-enabled"><span><span className="block">Rectangular crop</span><FieldDescription>Crop CSS-pixel coordinates from the final document.</FieldDescription></span></FieldLabel><Switch id="clip-enabled" checked={clipEnabled} onCheckedChange={(checked) => { setClipEnabled(checked); if (checked) setSelector("") }} disabled={!imageOutput} /></Field>
                        {clipEnabled && imageOutput && <div className="grid grid-cols-4 gap-2">
                          <Field><FieldLabel htmlFor="clip-x">X</FieldLabel><Input id="clip-x" type="number" min={0} max={100000} value={clipX} onChange={(event) => setClipX(Number(event.target.value))} /></Field>
                          <Field><FieldLabel htmlFor="clip-y">Y</FieldLabel><Input id="clip-y" type="number" min={0} max={100000} value={clipY} onChange={(event) => setClipY(Number(event.target.value))} /></Field>
                          <Field><FieldLabel htmlFor="clip-width">Width</FieldLabel><Input id="clip-width" type="number" min={1} max={100000} value={clipWidth} onChange={(event) => setClipWidth(Number(event.target.value))} /></Field>
                          <Field><FieldLabel htmlFor="clip-height">Height</FieldLabel><Input id="clip-height" type="number" min={1} max={100000} value={clipHeight} onChange={(event) => setClipHeight(Number(event.target.value))} /></Field>
                        </div>}
                      </FieldSet>
                      <Field><FieldLabel htmlFor="custom-css">Custom CSS</FieldLabel><InputGroup><InputGroupTextarea id="custom-css" rows={4} value={customCss} onChange={(event) => setCustomCss(event.target.value)} placeholder="header, .cookie-banner { display: none !important; }" /></InputGroup><FieldDescription>Applied to the main document for this render; maximum 64 KiB.</FieldDescription></Field>
                      <Field><FieldLabel htmlFor="fail-statuses">Fail on HTTP status</FieldLabel><Input id="fail-statuses" value={failStatuses} onChange={(event) => setFailStatuses(event.target.value)} placeholder="404,429,500,502,503" /><FieldDescription>Comma-separated exact status codes.</FieldDescription></Field>
                      <Field><FieldLabel>Lazy content loading</FieldLabel><Select value={lazyLoad} onValueChange={setLazyLoad}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="adaptive">Adaptive (default)</SelectItem><SelectItem value="thorough">Thorough (more complete)</SelectItem><SelectItem value="none">None (fastest)</SelectItem></SelectGroup></SelectContent></Select></Field>
                      <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><TimerIcon data-title-icon="wait-conditions" size={16} className="shrink-0 text-primary" aria-hidden="true" />Wait conditions</FieldLegend>
                        <div className="grid grid-cols-3 gap-3">
                          <Field><FieldLabel>Load event</FieldLabel><Select value={waitEvent} onValueChange={setWaitEvent}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="load">Load</SelectItem><SelectItem value="domcontentloaded">DOM ready</SelectItem><SelectItem value="networkidle">Network idle</SelectItem></SelectGroup></SelectContent></Select></Field>
                          <Field><FieldLabel htmlFor="delay">Wait (sec)</FieldLabel><Input id="delay" type="number" min={0} max={15} value={waitDelay} onChange={(event) => setWaitDelay(Number(event.target.value))} /></Field>
                          <Field><FieldLabel htmlFor="timeout">Timeout</FieldLabel><Input id="timeout" type="number" min={1} max={30} value={waitTimeout} onChange={(event) => setWaitTimeout(Number(event.target.value))} /></Field>
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                          <Field data-invalid={waitSelectorTouched && Boolean(waitSelectorError)}>
                            <FieldLabel htmlFor="wait-selector">Wait selector · <a href={`${docsUrl}#selectors-and-waits`} target="_blank" rel="noreferrer">Docs</a></FieldLabel>
                            <Input id="wait-selector" value={waitSelector} onChange={(event) => setWaitSelector(event.target.value)} onBlur={() => setWaitSelectorTouched(true)} placeholder=".ready" aria-invalid={waitSelectorTouched && Boolean(waitSelectorError)} />
                            {waitSelectorTouched && <FieldError>{waitSelectorError}</FieldError>}
                          </Field>
                          <Field><FieldLabel htmlFor="wait-text">Wait text</FieldLabel><Input id="wait-text" value={waitText} onChange={(event) => setWaitText(event.target.value)} placeholder="Loaded" /></Field>
                        </div>
                      </FieldSet>
                      {imageOutput && <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><ContrastIcon data-title-icon="image-encoding" size={16} className="shrink-0 text-primary" aria-hidden="true" />Image encoding</FieldLegend>
                        {(output === "jpeg" || output === "webp" || output === "avif") && <Field><FieldLabel htmlFor="quality">Image quality</FieldLabel><Input id="quality" type="number" min={1} max={100} value={quality} onChange={(event) => setQuality(Number(event.target.value))} /></Field>}
                        {(output === "png" || output === "webp" || output === "avif") && <Field orientation="horizontal"><FieldLabel htmlFor="transparent">Transparent background</FieldLabel><Switch id="transparent" checked={transparent} onCheckedChange={setTransparent} /></Field>}
                        {(output === "png" || output === "webp") && <Field orientation="horizontal"><FieldLabel htmlFor="optimize-image"><span><span className="block">Fast {output.toUpperCase()} encoding</span><FieldDescription>Prioritizes render speed over the smallest file size.</FieldDescription></span></FieldLabel><Switch id="optimize-image" checked={optimizePng} onCheckedChange={setOptimizePng} /></Field>}
                      </FieldSet>}
                      {(output === "html" || output === "markdown") && <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><BookTextIcon data-title-icon="content-extraction" size={16} className="shrink-0 text-primary" aria-hidden="true" />Content extraction</FieldLegend>
                        <Field><FieldLabel>Extraction</FieldLabel><Select value={extractMode} onValueChange={setExtractMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="document">Full document</SelectItem><SelectItem value="article">Main article</SelectItem></SelectGroup></SelectContent></Select></Field>
                      </FieldSet>}
                      {output === "pdf" && <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><FileTextIcon data-title-icon="pdf-layout" size={16} className="shrink-0 text-primary" aria-hidden="true" />PDF layout</FieldLegend>
                        <div className="grid grid-cols-3 gap-3">
                          <Field><FieldLabel>PDF mode</FieldLabel><Select value={pdfMode} onValueChange={setPdfMode}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="print">Print pages</SelectItem><SelectItem value="single_page">Single page</SelectItem></SelectGroup></SelectContent></Select></Field>
                          <Field><FieldLabel>Paper</FieldLabel><Select value={paperSize} onValueChange={setPaperSize}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="A4">A4</SelectItem><SelectItem value="Letter">Letter</SelectItem></SelectGroup></SelectContent></Select></Field>
                          <Field><FieldLabel>Orientation</FieldLabel><Select value={orientation} onValueChange={setOrientation}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="portrait">Portrait</SelectItem><SelectItem value="landscape">Landscape</SelectItem></SelectGroup></SelectContent></Select></Field>
                        </div>
                        <Field><FieldLabel htmlFor="pdf-margin">PDF margins (inches)</FieldLabel><Input id="pdf-margin" type="number" min={0} max={4} step={0.1} value={pdfMargin} onChange={(event) => setPdfMargin(Number(event.target.value))} /></Field>
                      </FieldSet>}
                      {videoOutput && <FieldSet>
                        <FieldLegend className="flex items-center gap-2"><ClapIcon data-title-icon="video-capture" size={16} className="shrink-0 text-primary" aria-hidden="true" />Video capture</FieldLegend>
                        <div className="grid grid-cols-3 items-end gap-3"><Field><FieldLabel htmlFor="video-duration">Duration (seconds)</FieldLabel><Input id="video-duration" type="number" min={1} max={30} value={videoDuration} onChange={(event) => setVideoDuration(Number(event.target.value))} /></Field><Field><FieldLabel htmlFor="video-fps">Frame rate (FPS)</FieldLabel><Input id="video-fps" type="number" min={1} max={60} value={videoFps} onChange={(event) => setVideoFps(Number(event.target.value))} /></Field>{output !== "gif" && <Field><FieldLabel htmlFor="video-bitrate">Bitrate (Mbps)</FieldLabel><Input id="video-bitrate" type="number" min={1} max={100} value={videoBitrate} onChange={(event) => setVideoBitrate(Number(event.target.value))} /></Field>}</div>{fullPage ? <FieldDescription>Full-page output scrolls from the top to the bottom at the chosen frame rate{output === "gif" ? "." : " and bitrate."}</FieldDescription> : <Field orientation="horizontal"><FieldLabel htmlFor="video-scroll">Scroll viewport while recording</FieldLabel><Switch id="video-scroll" checked={videoScroll} onCheckedChange={setVideoScroll} /></Field>}{fullPage && output !== "mp4" && <Field orientation="horizontal"><FieldLabel htmlFor="transparent-video">Transparent side padding</FieldLabel><Switch id="transparent-video" checked={transparent} onCheckedChange={setTransparent} /></Field>}
                      </FieldSet>}
                      <Field orientation="horizontal"><FieldLabel htmlFor="diagnostics"><span><span className="block">Diagnostic bundle</span><FieldDescription>ZIP the artifact with console and network reports.</FieldDescription></span></FieldLabel><Switch id="diagnostics" checked={diagnostics} onCheckedChange={setDiagnostics} /></Field>
                      <Field data-invalid={headersTouched && Boolean(headersValidation.error)}>
                        <FieldLabel htmlFor="headers">Same-origin headers · <a href={`${docsUrl}#custom-headers`} target="_blank" rel="noreferrer">Docs</a></FieldLabel>
                        <Input id="headers" value={headers} onChange={(event) => setHeaders(event.target.value)} onBlur={() => setHeadersTouched(true)} placeholder={'{"Authorization":"Bearer …"}'} aria-invalid={headersTouched && Boolean(headersValidation.error)} />
                        <FieldDescription>Sent only to the exact target origin. <a href={siteAccessUrl} target="_blank" rel="noreferrer">Authorize a site you control.</a></FieldDescription>
                        {headersTouched && <FieldError>{headersValidation.error}</FieldError>}
                      </Field>
                    </FieldGroup>
                  </CollapsibleContent>
                </Collapsible>
              </FieldGroup>
            </aside>

            <section className="flex min-w-0 flex-col bg-card p-4 sm:p-6">
              <div className="mb-4 flex items-center justify-between">
                <div className="flex items-start gap-2.5">
                  <EyeIcon data-title-icon="result" size={18} className="mt-0.5 shrink-0 text-primary" aria-hidden="true" />
                  <div><p className="font-medium">Result</p><p className="text-xs text-muted-foreground">Preview and download your latest capture.</p></div>
                </div>
                <Badge variant={status === "Complete" ? "default" : "secondary"}>{status}</Badge>
              </div>
              <div className="subtle-grid flex min-h-[420px] flex-1 items-center justify-center overflow-hidden rounded-xl border bg-muted/20 p-4">
                {busy ? (
                  <Card className="w-full max-w-md text-center" aria-live="polite">
                    <CardHeader>
                      <Loader2 className="mx-auto size-8 animate-spin text-primary" />
                      <CardTitle>{progressStage}</CardTitle>
                      <CardDescription>{formatDuration(Math.round(elapsedMs))} elapsed · live estimate</CardDescription>
                    </CardHeader>
                    <CardContent className="flex flex-col gap-4 text-left">
                      <Progress value={progressValue} aria-label="Estimated capture progress" />
                      <div className="rounded-lg border bg-muted/30 p-3">
                        <p className="text-xs font-medium">Active wait plan</p>
                        <p className="mt-1 text-xs leading-5 text-muted-foreground">{activeRun?.waitConditions ?? waitConditions}</p>
                      </div>
                    </CardContent>
                    <CardFooter className="justify-center">
                      <Button variant="outline" onClick={cancelCapture}>
                        <Square data-icon="inline-start" />
                        Cancel capture
                      </Button>
                    </CardFooter>
                  </Card>
                ) : latest && latest.type.startsWith("image/") ? (
                  <img src={latest.url} alt="Latest ViperCapture result" className="max-h-[580px] max-w-full rounded-lg border bg-background object-contain shadow-xl" />
                ) : latest ? (
                  <div className="max-w-sm text-center"><div className="mx-auto flex size-12 items-center justify-center rounded-xl border bg-background"><Download className="size-5 text-muted-foreground" /></div><p className="mt-4 font-medium">{latest.name}</p><p className="mt-1 text-sm leading-6 text-muted-foreground">This output is ready to open or download below.</p></div>
                ) : (
                  <div className="max-w-sm text-center"><div className="mx-auto flex size-12 items-center justify-center rounded-xl border bg-background"><ImageIcon className="size-5 text-muted-foreground" /></div><p className="mt-4 font-medium">Your capture will appear here</p><p className="mt-1 text-sm leading-6 text-muted-foreground">Choose a URL and capture settings, then run the renderer.</p></div>
                )}
              </div>
              {latest && !busy && (
                <Card className="mt-4">
                  <CardHeader>
                    <CardTitle className="truncate text-base">{latest.name}</CardTitle>
                    <CardDescription>{latest.type || output.toUpperCase()}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <dl className="grid grid-cols-2 gap-4 xl:grid-cols-4">
                      <ResultMetric label="Dimensions" value={latest.width && latest.height ? `${latest.width} × ${latest.height} px` : "Not reported"} />
                      <ResultMetric label="File size" value={formatBytes(latest.sizeBytes)} />
                      <ResultMetric label="Render duration" value={latest.renderMs === undefined ? "Not reported" : formatDuration(latest.renderMs)} />
                      <ResultMetric label="Request ID" value={latest.requestId ?? "Not reported"} mono />
                    </dl>
                  </CardContent>
                  <CardFooter className="justify-end gap-2">
                    {!latest.type.startsWith("text/html") && <Button variant="outline" size="sm" asChild><a href={latest.url} target="_blank" rel="noreferrer"><ExternalLink data-icon="inline-start" />Open</a></Button>}
                    <Button size="sm" asChild><a href={latest.url} download={latest.name}><Download data-icon="inline-start" />Download</a></Button>
                  </CardFooter>
                </Card>
              )}
              {history.length > 1 && (
                <div className="mt-5 border-t pt-4">
                  <p className="mono-label mb-3">Recent in this tab</p>
                  <div className="flex gap-2 overflow-x-auto pb-1">
                    {history.map((item) => <a key={item.url} href={item.url} download={item.name} className="flex min-w-36 items-center gap-2 rounded-lg border bg-background p-2 text-xs hover:bg-accent"><CheckCircle2 className="size-4 text-primary" /><span className="truncate">{item.name}</span></a>)}
                  </div>
                </div>
              )}
            </section>
          </div>
        </section>

        <footer className="flex flex-col gap-3 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>Open source under the MIT License.</p>
          <Button variant="link" size="sm" asChild><a href="https://capture.viperisuseful.cc" target="_blank" rel="noreferrer">Try ViperCapture Cloud <ExternalLink data-icon="inline-end" /></a></Button>
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

      <AlertDialog open={savePresetOpen} onOpenChange={setSavePresetOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Save capture preset</AlertDialogTitle>
            <AlertDialogDescription>Saved on this machine.</AlertDialogDescription>
          </AlertDialogHeader>
          <Field>
            <FieldLabel htmlFor="preset-name">Preset name</FieldLabel>
            <Input
              id="preset-name"
              value={presetName}
              onChange={(event) => setPresetName(event.target.value)}
              placeholder="e.g. Blog article"
              maxLength={40}
            />
          </Field>
          {savedPresets.length > 0 && (
            <ul className="flex flex-col gap-1">
              {savedPresets.map((preset) => (
                <li key={preset.name} className="flex items-center justify-between gap-2 text-sm">
                  <span className="truncate">{preset.name}</span>
                  <Button size="icon-sm" variant="ghost" aria-label={`Delete ${preset.name}`} onClick={() => deletePreset(preset.name)}>
                    <Trash2 />
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>Close</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void savePreset()}
              disabled={!presetName.trim() || savedPresets.some((preset) => preset.name === presetName.trim())}
            >
              Save preset
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
