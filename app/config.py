# ============================================================
# 权限感知型私有科研知识库问答系统 —— 运行配置
# 全部使用 Python 3.10+ 标准库，零强制第三方依赖。
# 若环境中安装了以下可选包，将自动启用更好的解析效果：
#   pypdf       -> PDF 文本提取（否则使用内置简易 PDF 提取器）
#   python-docx -> DOCX 文本提取（否则使用内置 ZIP/XML 提取器）
# ============================================================

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("KB_DATA_DIR", BASE_DIR / "data"))
TENANT_DIR = DATA_DIR / "tenants"          # 每租户独立 SQLite 库
BLOB_DIR = DATA_DIR / "blobs"             # 每租户独立文件目录
GLOBAL_DB = DATA_DIR / "global.db"        # 仅存租户/账号/口令/令牌

WEB_DIR = BASE_DIR / "web"

# 服务
HOST = os.environ.get("KB_HOST", "0.0.0.0")
PORT = int(os.environ.get("KB_PORT", "8080"))

# 分段参数（语义自适应分段，默认约 900 字，上限 1400 字）
CHUNK_TARGET_CHARS = int(os.environ.get("KB_CHUNK_TARGET", "900"))
CHUNK_MAX_CHARS = int(os.environ.get("KB_CHUNK_MAX", "1400"))
# 允许作为分节起点的标题最大长度（防止把正文误判为标题）
HEADING_MAX_CHARS = 40

# 检索
SEARCH_TOP_K = int(os.environ.get("KB_TOP_K", "8"))
BM25_K1 = 1.5
BM25_B = 0.75

# LLM（可选）。不配置时使用内置抽取式问答（无需联网/密钥）
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")           # 如 https://api.openai.com/v1
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30"))

SUPPORTED_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".log"}
MAX_UPLOAD_MB = int(os.environ.get("KB_MAX_UPLOAD_MB", "100"))
