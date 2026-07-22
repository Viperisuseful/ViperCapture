# Viper Full-Page Capture skill

1. Copy the `viper-full-page-capture` folder into the user's Codex skills directory:

   `C:\Users\<user>\.codex\skills\`

2. Set the API key on the new computer. Create `C:\Users\<user>\.env` from `.env.example`, or set the `VIPERCAPTURE_API_KEY` environment variable directly.

3. Start a new Codex task or refresh the skills list. The skill can then be invoked with `$viper-full-page-capture` or by asking for a full-page capture of a public URL.

Example command:

```powershell
python "C:\Users\<user>\.codex\skills\viper-full-page-capture\scripts\capture.py" `
  --url "https://example.com" --output png --output-dir .
```

The real API key is intentionally not included in this transfer package.
