import urllib.request
import time

def take_screenshot(url, filename):
    api_url = f"http://127.0.0.1:8000/screenshot?url={url}&wait=10&device_scale_factor=2"
    print(f"Requesting screenshot for {url}...")
    try:
        with urllib.request.urlopen(api_url, timeout=30) as response:
            with open(filename, 'wb') as f:
                f.write(response.read())
        print(f"Saved {filename}")
    except Exception as e:
        print(f"Failed to capture {url}: {e}")

# Wait for server to be fully ready
time.sleep(3)

take_screenshot("https://turtlecave.xyz", "turtlecave.png")
take_screenshot("https://quickrunlab.tech", "quickrunlab.png")
