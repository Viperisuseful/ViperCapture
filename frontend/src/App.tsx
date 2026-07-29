import { useEffect, useMemo, useRef, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Download,
  ExternalLink,
  Gauge,
  Code2,
  Globe2,
  ImageIcon,
  Loader2,
  Moon,
  RotateCcw,
  SlidersHorizontal,
  Sparkles,
  Square,
  Sun,
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
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupTextarea } from "@/components/ui/input-group"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

type Output = "png" | "jpeg" | "webp"
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
  gpu?: { mode?: "off" | "auto" | "required"; hardware_active?: boolean; mutable?: boolean }
}
type CaptchaWarning = { provider: string; requestId?: string }

const presets = [
  ["Phone", 390, 844],
  ["Tablet", 768, 1024],
  ["HD", 1280, 720],
  ["Full HD", 1920, 1080],
  ["2K", 2560, 1440],
  ["4K", 3840, 2160],
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
const blockedHeaderNames = new Set([
  "connection", "content-length", "forwarded", "host", "keep-alive",
  "proxy-authenticate", "proxy-authorization", "te", "trailer",
  "transfer-encoding", "upgrade",
])
const blockedHeaderPrefixes = ["proxy-", "sec-", "x-forwarded-"]
const headerNamePattern = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

function extension(output: Output) {
  return output === "jpeg" ? "jpg" : output
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
  const [dark, setDark] = useState(() => localStorage.getItem("theme") === "dark")
  const [url, setUrl] = useState("https://example.com")
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
  const [waitEvent, setWaitEvent] = useState("load")
  const [waitDelay, setWaitDelay] = useState(1)
  const [waitSelector, setWaitSelector] = useState("")
  const [waitText, setWaitText] = useState("")
  const [waitTimeout, setWaitTimeout] = useState(15)
  const [headers, setHeaders] = useState("")
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
  const historyRef = useRef<Capture[]>([])
  const abortControllerRef = useRef<AbortController | null>(null)
  const captureStartedAtRef = useRef(0)

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark)
    localStorage.setItem("theme", dark ? "dark" : "light")
  }, [dark])
  useEffect(() => {
    fetch("/app-config").then((response) => response.json()).then(setConfig).catch(() => null)
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

  const maxPixels = config?.max_screenshot_pixels ?? 50_000_000
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

  const reset = () => {
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
    setWaitEvent("load")
    setWaitDelay(1)
    setWaitSelector("")
    setWaitText("")
    setWaitTimeout(15)
    setHeaders("")
    setHeadersTouched(false)
    setSelectorTouched(false)
    setWaitSelectorTouched(false)
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
    const customHeaders = headersValidation.headers
    const normalized = /^https?:\/\//i.test(url.trim()) ? url.trim() : `https://${url.trim()}`
    const payload = {
      url: normalized,
      output,
      viewport: { width, height, device_scale_factor: density },
      full_page: selector ? false : fullPage,
      preserve_viewport_width: !selector && fullPage && preserveViewportWidth,
      lazy_load: lazyLoad,
      selector: selector || null,
      image: {
        quality: output === "png" ? null : quality,
        transparent_background: output !== "jpeg" && transparent,
        optimize_for_speed: output === "png" && optimizePng,
      },
      headers: customHeaders,
      wait_for: {
        event: waitEvent,
        selector: waitSelector || null,
        text: waitText || null,
        delay_ms: waitDelay * 1000,
        timeout_ms: waitTimeout * 1000,
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
        headers: { "Content-Type": "application/json" },
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
        <section className="mb-8 grid gap-6 lg:grid-cols-[1fr_auto] lg:items-end">
          <div className="max-w-3xl">
            <Badge variant="outline" className="mb-4"><Sparkles data-icon="inline-start" />Local Chromium renderer</Badge>
            <h1 className="page-title">The whole webpage, in one capture.</h1>
            <p className="mt-4 max-w-2xl text-base leading-7 text-muted-foreground sm:text-lg">
              Render a public URL as PNG, JPEG, or WebP with the same focused workspace as ViperCapture Cloud.
            </p>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {[
              [Gauge, "Local", "Private"],
              [Zap, "Fast", "Chromium"],
              [ImageIcon, "3", "Formats"],
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
            <Field className="min-w-0 flex-1">
              <FieldLabel htmlFor="capture-url">Website URL</FieldLabel>
              <InputGroup className="h-auto min-h-10">
                <InputGroupAddon><Globe2 /></InputGroupAddon>
                <InputGroupTextarea id="capture-url" rows={1} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com" className="min-h-10" />
              </InputGroup>
            </Field>
            <Button size="lg" onClick={() => void capture()} disabled={busy} className="lg:min-w-36">
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
                    <Tabs value={output} onValueChange={(value: string) => setOutput(value as Output)}>
                      <TabsList className="w-full"><TabsTrigger value="png">PNG</TabsTrigger><TabsTrigger value="jpeg">JPEG</TabsTrigger><TabsTrigger value="webp">WebP</TabsTrigger></TabsList>
                    </Tabs>
                  </Field>
                </FieldSet>

                <FieldSet>
                  <FieldLegend>Viewport</FieldLegend>
                  <div className="grid grid-cols-3 gap-3">
                    <Field><FieldLabel htmlFor="width">Width</FieldLabel><Input id="width" type="number" min={1} max={7680} value={width} onChange={(event) => setWidth(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="height">Height</FieldLabel><Input id="height" type="number" min={1} max={4320} value={height} onChange={(event) => setHeight(Number(event.target.value))} /></Field>
                    <Field><FieldLabel htmlFor="density">Density</FieldLabel><Input id="density" type="number" min={0.1} max={4} step={0.25} value={density} onChange={(event) => setDensity(Number(event.target.value))} /></Field>
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
                    <Switch id="full-page" checked={fullPage} onCheckedChange={(checked: boolean) => setFullPage(checked)} disabled={Boolean(selector)} />
                  </Field>
                  {fullPage && !selector && (
                    <Field orientation="horizontal">
                      <FieldLabel htmlFor="preserve-width"><span><span className="block">Preserve viewport width</span><FieldDescription>Clip horizontal overflow while keeping the full page height.</FieldDescription></span></FieldLabel>
                      <Switch id="preserve-width" checked={preserveViewportWidth} onCheckedChange={setPreserveViewportWidth} />
                    </Field>
                  )}
                </FieldSet>

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

                <Collapsible>
                  <CollapsibleTrigger asChild>
                    <Button variant="outline" className="w-full justify-between"><span className="flex items-center gap-2"><SlidersHorizontal />Advanced controls</span><ChevronDown /></Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-4">
                    <FieldGroup>
                      <Field data-invalid={selectorTouched && Boolean(selectorError)}>
                        <FieldLabel htmlFor="selector">Element selector · <a href={`${docsUrl}#selectors-and-waits`} target="_blank" rel="noreferrer">Docs</a></FieldLabel>
                        <Input id="selector" value={selector} onChange={(event) => setSelector(event.target.value)} onBlur={() => setSelectorTouched(true)} placeholder="main, #invoice" aria-invalid={selectorTouched && Boolean(selectorError)} />
                        {selectorTouched && <FieldError>{selectorError}</FieldError>}
                      </Field>
                      <Field><FieldLabel>Lazy content loading</FieldLabel><Select value={lazyLoad} onValueChange={setLazyLoad}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="thorough">Thorough (default)</SelectItem><SelectItem value="adaptive">Adaptive (faster)</SelectItem><SelectItem value="none">None (fastest)</SelectItem></SelectGroup></SelectContent></Select></Field>
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
                      {output !== "png" && <Field><FieldLabel htmlFor="quality">Image quality</FieldLabel><Input id="quality" type="number" min={1} max={100} value={quality} onChange={(event) => setQuality(Number(event.target.value))} /></Field>}
                      {output !== "jpeg" && <Field orientation="horizontal"><FieldLabel htmlFor="transparent">Transparent background</FieldLabel><Switch id="transparent" checked={transparent} onCheckedChange={setTransparent} /></Field>}
                      {output === "png" && <Field orientation="horizontal"><FieldLabel htmlFor="optimize-png"><span><span className="block">Fast PNG encoding</span><FieldDescription>Uses less encoding work, with a larger file.</FieldDescription></span></FieldLabel><Switch id="optimize-png" checked={optimizePng} onCheckedChange={setOptimizePng} /></Field>}
                      <Field data-invalid={headersTouched && Boolean(headersValidation.error)}>
                        <FieldLabel htmlFor="headers">Same-origin headers · <a href={`${docsUrl}#custom-headers`} target="_blank" rel="noreferrer">Docs</a></FieldLabel>
                        <Input id="headers" value={headers} onChange={(event) => setHeaders(event.target.value)} onBlur={() => setHeadersTouched(true)} placeholder={'{"Authorization":"Bearer …"}'} aria-invalid={headersTouched && Boolean(headersValidation.error)} />
                        <FieldDescription>Sent only to the exact target origin.</FieldDescription>
                        {headersTouched && <FieldError>{headersValidation.error}</FieldError>}
                      </Field>
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
                ) : latest ? (
                  <img src={latest.url} alt="Latest ViperCapture result" className="max-h-[580px] max-w-full rounded-lg border bg-background object-contain shadow-xl" />
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
                    <Button variant="outline" size="sm" asChild><a href={latest.url} target="_blank" rel="noreferrer"><ExternalLink data-icon="inline-start" />Open</a></Button>
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
    </div>
  )
}
