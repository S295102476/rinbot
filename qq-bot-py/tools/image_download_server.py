"""
本地图片下载服务器
浏览器打开 http://localhost:9999 后自动通过浏览器环境下载所有图片

用法: py tools/image_download_server.py
然后浏览器打开 http://localhost:9999
"""
import os
import json
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(TOOLS_DIR, "wiki_images")
URLS_FILE = os.path.join(TOOLS_DIR, "image_urls.txt")

os.makedirs(IMAGES_DIR, exist_ok=True)

with open(URLS_FILE, "r", encoding="utf-8") as f:
    ALL_URLS = [line.strip() for line in f if line.strip()]

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Wiki 图片批量下载</title>
<style>
body { font-family: sans-serif; padding: 20px; background: #1a1a2e; color: #eee; }
#status { font-size: 18px; margin: 10px 0; }
#progress { width: 100%%; height: 30px; background: #333; border-radius: 5px; overflow: hidden; }
#bar { height: 100%%; background: #4ecca3; transition: width 0.3s; width: 0%%; }
#log { margin-top: 15px; height: 400px; overflow-y: auto; background: #16213e; padding: 10px; 
       border-radius: 5px; font-size: 13px; font-family: monospace; }
.ok { color: #4ecca3; } .fail { color: #e74c3c; } .skip { color: #f39c12; }
</style></head><body>
<h2>Wiki 图片批量下载 (共 %(total)d 张)</h2>
<div id="status">准备中...</div>
<div id="progress"><div id="bar"></div></div>
<div id="log"></div>
<script>
const URLS = %(urls_json)s;
const TOTAL = URLS.length;
const CONCURRENT = 5;
let done = 0, ok = 0, fail = 0, skip = 0;

function log(msg, cls) {
  const d = document.getElementById('log');
  d.innerHTML += `<div class="${cls}">${msg}</div>`;
  d.scrollTop = d.scrollHeight;
}

function updateStatus() {
  document.getElementById('status').textContent = 
    `进度: ${done}/${TOTAL}  成功: ${ok}  跳过: ${skip}  失败: ${fail}`;
  document.getElementById('bar').style.width = (done/TOTAL*100) + '%%';
}

async function downloadOne(url) {
  const filename = decodeURIComponent(url.split('/').pop());
  try {
    // 先检查服务器是否已有
    const chk = await fetch('/check?name=' + encodeURIComponent(filename));
    const chkData = await chk.json();
    if (chkData.exists) {
      skip++; done++;
      log(`跳过 ${filename} (已存在)`, 'skip');
      updateStatus();
      return;
    }
    
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    if (blob.size < 500) throw new Error(`太小 ${blob.size}B`);
    
    const reader = new FileReader();
    const b64 = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result.split(',')[1]);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    
    const saveResp = await fetch('/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename, data: b64})
    });
    
    if (saveResp.ok) {
      ok++; log(`✓ ${filename} (${(blob.size/1024).toFixed(1)}KB)`, 'ok');
    } else {
      throw new Error('保存失败');
    }
  } catch(e) {
    fail++; log(`✗ ${filename}: ${e.message}`, 'fail');
  }
  done++;
  updateStatus();
}

async function run() {
  log('开始下载...', 'ok');
  updateStatus();
  
  let i = 0;
  async function worker() {
    while (i < TOTAL) {
      const idx = i++;
      await downloadOne(URLS[idx]);
    }
  }
  
  const workers = [];
  for (let w = 0; w < CONCURRENT; w++) workers.push(worker());
  await Promise.all(workers);
  
  log(`\\n===== 完成! 成功: ${ok}, 跳过: ${skip}, 失败: ${fail} =====`, 'ok');
  document.getElementById('status').textContent = 
    `✅ 完成!  成功: ${ok}  跳过: ${skip}  失败: ${fail}`;
}

run();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            urls_json = json.dumps(ALL_URLS, ensure_ascii=False)
            html = HTML_PAGE % {"total": len(ALL_URLS), "urls_json": urls_json}
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        elif self.path.startswith('/check'):
            name = unquote(self.path.split('name=')[1]) if 'name=' in self.path else ''
            exists = os.path.isfile(os.path.join(IMAGES_DIR, name)) if name else False
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"exists": exists}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/save':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            filename = body['filename'].replace('/', '_').replace('\\', '_')
            data = base64.b64decode(body['data'])
            filepath = os.path.join(IMAGES_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(data)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默日志


if __name__ == '__main__':
    print(f"图片总数: {len(ALL_URLS)}")
    print(f"保存目录: {IMAGES_DIR}")
    print(f"\n请在浏览器中打开: http://localhost:9999")
    print("下载完成后按 Ctrl+C 关闭服务器\n")
    server = HTTPServer(('127.0.0.1', 9999), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已关闭")
        count = len([f for f in os.listdir(IMAGES_DIR) if os.path.isfile(os.path.join(IMAGES_DIR, f))])
        print(f"已下载 {count} 张图片")
