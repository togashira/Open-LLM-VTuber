import os
import sys
import atexit
import argparse
from pathlib import Path
import tomli
import uvicorn
from loguru import logger
from upgrade import sync_user_config, select_language
from src.open_llm_vtuber.server import WebSocketServer
from src.open_llm_vtuber.config_manager import Config, read_yaml, validate_config
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from src.open_llm_vtuber.live2d_mount_guard import Live2DGuard
app = FastAPI()



port = getattr(cfg.system_config, "port", 12393)
live2d_dir = getattr(cfg.frontend, "live2d_model_path",
                     "/mnt/data/Open-LLM-VTuber/Open-LLM-VTuber/live2d-models")
model = getattr(cfg.character_config, "live2d_model_name", "shizuku-local")

guard = Live2DGuard(app, mount_path="/live2d-models", base_dir=live2d_dir, model_name=model)

@app.on_event("startup")
async def _start_live2d_guard():
    guard.start_watch(port=port, interval_sec=60.0)  # 60秒毎に健全性チェック


@app.get("/")
async def root():
  return {"status": "healthy"}  # ヘルスチェック用200レスポンス

# modelURL固定 2025/7/21
app.mount("/live2d-models", StaticFiles(directory="/mnt/data/Open-LLM-VTuber/Open-LLM-VTuber/live2d-models", html=True), name="live2d")
#ここまで

os.environ["HF_HOME"] = str(Path(__file__).parent / "models")
os.environ["MODELSCOPE_CACHE"] = str(Path(__file__).parent / "models")


def get_version() -> str:
    with open("pyproject.toml", "rb") as f:
        pyproject = tomli.load(f)
    return pyproject["project"]["version"]


def init_logger(console_log_level: str = "INFO") -> None:
    logger.remove()
    # Console output
    logger.add(
        sys.stderr,
        level=console_log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | {message}",
        colorize=True,
    )

    # File output
    logger.add(
        "logs/debug_{time:YYYY-MM-DD}.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message} | {extra}",
        backtrace=True,
        diagnose=True,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Open-LLM-VTuber Server")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--hf_mirror", action="store_true", help="Use Hugging Face mirror"
    )
    return parser.parse_args()


@logger.catch
def run(console_log_level: str):
    init_logger(console_log_level)
    logger.info(f"Open-LLM-VTuber, version v{get_version()}")
    # Sync user config with default config
    try:
        sync_user_config(logger=logger, lang=select_language())
    except Exception as e:
        logger.error(f"Error syncing user config: {e}")

    atexit.register(WebSocketServer.clean_cache)

    # Load configurations from yaml file
    config: Config = validate_config(read_yaml("conf.yaml"))
    server_config = config.system_config

    # Initialize and run the WebSocket server
    server = WebSocketServer(config=config)
    uvicorn.run(
        app=server.app,
        host=server_config.host,
        port=server_config.port,
        log_level=console_log_level.lower(),
    )


if __name__ == "__main__":
    args = parse_args()
    console_log_level = "DEBUG" if args.verbose else "INFO"
    if args.verbose:
        logger.info("Running in verbose mode")
    else:
        logger.info(
            "Running in standard mode. For detailed debug logs, use: uv run run_server.py --verbose"
        )
    if args.hf_mirror:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    run(console_log_level=console_log_level)
