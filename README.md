# Alltuu Photo Downloader / 喔图相册下载整理工具

这是一个面向 Alltuu（喔图）相册的命令行下载整理工具。网页端在批量操作时仍可能向浏览器逐个提交文件；这个程序把用户已有权限范围内的下载任务整理成一个可恢复的本地队列，并自动处理分段、文件名和失败重试。

This command-line tool organizes downloads from Alltuu albums that the user is already authorized to access. It turns the browser-accessible download workflow into a resumable local queue with segment discovery, predictable filenames, progress reporting, and retry handling.

> This is an independent, unofficial project. It is not affiliated with or endorsed by Alltuu.
>
> 本项目为非官方独立工具，与喔图平台不存在隶属、合作或背书关系。

## What It Does

- Discovers the date or photographer segments exposed by the loaded album page.
- Selects the highest-quality HTTPS image URL made available to the page.
- Downloads with a conservative default of four concurrent requests.
- Writes to `.part` files and renames them only after a complete response.
- Reuses deterministic filenames and skips completed files on later runs.
- Respects `Retry-After` responses and records failures in `failed-downloads.json`.
- Restricts album input to HTTPS Alltuu URLs or a bare 32-character album ID.

The program does not log in for the user, defeat passwords, remove watermarks, or grant access to unavailable content.

## Requirements

- Python 3.9 or newer
- Microsoft Edge
- Python packages listed in `requirements.txt`

Recent Selenium versions normally obtain the matching Edge driver through Selenium Manager. If that fails, install a compatible [Microsoft Edge WebDriver](https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/) and place it on `PATH`.

## Installation

```bash
git clone https://github.com/TianBingzhuo/alltuu-downloader.git
cd alltuu-downloader
python -m pip install -r requirements.txt
```

## Usage

```bash
python alltuu_downloader.py "https://m.alltuu.com/album/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/?menu=live"
```

The bare album ID is also accepted:

```bash
python alltuu_downloader.py "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

Useful options:

```text
-o, --output DIR          Base output directory
-w, --workers N           Concurrent downloads, from 1 to 16 (default: 4)
-t, --timeout SECONDS     Per-request timeout (default: 60)
--retries N               Attempts per photo (default: 3)
--request-delay SECONDS   Delay before each request (default: 0.15)
--no-headless             Show the Edge window for debugging
```

Examples:

```bash
# Save under a chosen base directory
python alltuu_downloader.py "<authorized-album-url>" -o ~/Photos

# Use two workers on a slower or shared connection
python alltuu_downloader.py "<authorized-album-url>" -w 2 --request-delay 0.5

# Show the browser while diagnosing a page-load problem
python alltuu_downloader.py "<authorized-album-url>" --no-headless
```

## How It Works

The program loads the supplied album page in Edge and observes photo metadata that the page itself receives. It discovers available album segments, switches or scrolls through those segments, and collects the image URLs exposed to the current browser session. The downloader then saves those files through `aiohttp`.

Alltuu's page structure is not a public API contract, so a future website update may require corresponding changes here. Version 3.2.0 is designed around the page structure observed in June 2026.

## Resume and Failure Handling

Target names are assigned before concurrent downloads start. Duplicate source names receive a stable photo ID or sequence suffix, which prevents workers from overwriting one another.

Each response is streamed to `filename.ext.part`. The temporary file is replaced atomically only after enough data has been received. Running the same command again skips completed targets and retries missing ones. Remaining failures are summarized without storing signed download URLs:

```text
album-folder/
├── photo.jpg
├── duplicate_12345.jpg
└── failed-downloads.json
```

## Testing

The repository includes offline regression tests for URL validation, safe output paths, duplicate naming, retry parsing, streaming writes, and resume behavior:

```bash
python -m unittest discover -s tests -v
python -m py_compile alltuu_downloader.py
```

An end-to-end test requires an album that the tester is authorized to access and is intentionally not part of the automated test suite.

## Responsible Use

Use this program only for albums and files that you own or are authorized to download. Public visibility of a link does not by itself transfer copyright or waive the privacy rights of people shown in a photograph.

Please follow:

- the Alltuu user agreement and any album-specific access rules;
- photographers' copyright and licensing terms;
- portrait, privacy, and personal-information requirements;
- reasonable request rates that do not disrupt the platform.

Do not use this project to evade authentication, access controls, payment requirements, watermarks, or platform restrictions. Repository maintainers do not provide downloaded photographs, private links, credentials, or support for unauthorized collection.

## 中文使用说明

### 主要功能

- 自动整理当前页面能够访问到的相册分段；
- 优先选择页面提供的原图、大图或其他可用 HTTPS 地址；
- 默认四路并发，可主动降低并发和增加请求间隔；
- 使用临时文件写入，下载完整后再正式保存；
- 文件名稳定，重复运行时跳过已经完成的文件；
- 对限流响应进行等待，失败项目写入本地清单；
- 仅接受喔图 HTTPS 相册链接或 32 位相册 ID。

### 基本命令

```bash
python alltuu_downloader.py "https://m.alltuu.com/album/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/?menu=live"
```

网络较慢或不希望请求过密时：

```bash
python alltuu_downloader.py "<你有权访问的相册链接>" -w 2 --request-delay 0.5
```

### 使用边界

这个工具不会替用户登录，不会破解密码，不会去除水印，也不会让浏览器取得原本不可访问的照片。请只下载本人拥有或已经取得授权的内容，并遵守平台规则、摄影师版权和照片中人物的隐私权。

## License

Code in this repository is released under the [Apache License 2.0](LICENSE). The license applies to the source code, not to photographs downloaded by a user.
