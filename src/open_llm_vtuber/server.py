
import os
import shutil
import logging
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .routes import init_client_ws_route, init_webtool_routes
from .service_context import ServiceContext
from .config_manager.utils import Config
from starlette.middleware.base import BaseHTTPMiddleware
import time



class SecurityMiddleware(BaseHTTPMiddleware):
    """IP制限とセキュリティスキャン対策のミドルウェア"""
    
    def __init__(self, app, allowed_ips=None, blocked_ips=None):
        super().__init__(app)
        self.allowed_ips = allowed_ips or []
        # プライベートIPとパブリックIP両方をブロック
        self.blocked_ips = blocked_ips or [
            "10.0.0.57",  # プライベートIP
            # 実際の攻撃元パブリックIPが判明次第追加
        ]

        # ログディレクトリとロガー初期化
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, "debug.log"),
            when="midnight",
            backupCount=7,
            encoding="utf-8"
        )
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        self.logger = logging.getLogger("SecurityMiddleware")
        self.logger.setLevel(logging.INFO)
        if not self.logger.hasHandlers():
            self.logger.addHandler(handler)
        
        # 悪意のあるパスパターン
        self.malicious_patterns = [
            '.php', 'wp-admin', 'wp-content', 'wp-includes', 
            'admin.php', 'shell.php', 'filemanager.php',
            '.well-known', 'xmlrpc.php'
        ]
        
        # AIクローラー・ボットのUser-Agentパターン
        self.ai_bot_patterns = [
            'gptbot', 'chatgpt', 'openai', 'anthropic', 'claude',
            'bingbot', 'bard', 'palm', 'llama', 'meta-ai',
            'scrapy', 'selenium', 'crawl', 'spider', 'bot',
            'python-requests', 'curl/', 'wget/', 'httpx'
        ]
    
    async def dispatch(self, request: Request, call_next):
        from datetime import datetime
        
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "").lower()
        path = request.url.path
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # X-Forwarded-For ヘッダーから実際のパブリックIPを取得
        forwarded_for = request.headers.get("x-forwarded-for", "")
        real_ip = forwarded_for.split(',')[0].strip() if forwarded_for else client_ip
        
        # デバッグログ: 全リクエストを記録（タイムスタンプ付き）
        self.logger.info(f"🔍 Request: {real_ip} (via {client_ip}) -> {path} (UA: {user_agent[:50]}...)")
        if forwarded_for:
            self.logger.info(f"📡 X-Forwarded-For: {forwarded_for}")
        
        # ブロックリストのIPをチェック（パブリックIPとプライベートIP両方）
        if client_ip in self.blocked_ips or real_ip in self.blocked_ips:
            self.logger.warning(f"🚫 IP Blocked: {real_ip} (via {client_ip})")
            return Response("Access Denied", status_code=403)
        
        # AIボット・クローラーのUser-Agentをチェック
        if any(pattern in user_agent for pattern in self.ai_bot_patterns):
            self.logger.warning(f"🤖 AI Bot blocked: {real_ip} -> {user_agent}")
            return Response("AI crawling not allowed", status_code=403)
        
        # 悪意のあるパスパターンをチェック
        if any(pattern in path.lower() for pattern in self.malicious_patterns):
            self.logger.warning(f"🚨 Malicious request blocked: {real_ip} -> {path}")
            return Response("Not Found", status_code=404)
        
        # 正常リクエストの場合
        response = await call_next(request)
        self.logger.info(f"✅ Request OK: {path} -> {response.status_code}")
        return response

class UserRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests=3, period=1):
        super().__init__(app)
        self.max_requests = max_requests
        self.period = period
        self.user_requests = {}

    async def dispatch(self, request: Request, call_next):
        user_id = request.client.host  # IPアドレス単位。認証ユーザーIDでもOK
        now = time.time()
        reqs = self.user_requests.get(user_id, [])
        reqs = [t for t in reqs if now - t < self.period]
        if len(reqs) >= self.max_requests:
            raise HTTPException(status_code=429, detail="Too Many Requests (per user)")
        reqs.append(now)
        self.user_requests[user_id] = reqs
        return await call_next(request)

class CustomStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".js"):
            response.headers["Content-Type"] = "application/javascript"
        return response


class AvatarStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        allowed_extensions = (".jpg", ".jpeg", ".png", ".gif", ".svg")
        if not any(path.lower().endswith(ext) for ext in allowed_extensions):
            return Response("Forbidden file type", status_code=403)
        return await super().get_response(path, scope)


class WebSocketServer:
    def __init__(self, config: Config):
        self.app = FastAPI()

        # セキュリティミドルウェアを最初に追加
        self.app.add_middleware(SecurityMiddleware)
        # ★ここで追加
        self.app.add_middleware(UserRateLimitMiddleware, max_requests=3, period=1)

        # Add CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Load configurations and initialize the default context cache
        default_context_cache = ServiceContext()
        default_context_cache.load_from_config(config)

        # Include WebSocket routes FIRST before any static file mounts
        self.app.include_router(
            init_client_ws_route(default_context_cache=default_context_cache),
        )
        self.app.include_router(
            init_webtool_routes(default_context_cache=default_context_cache),
        )


        # Mount cache directory first (to ensure audio file access)
        if not os.path.exists("cache"):
            os.makedirs("cache")
        self.app.mount(
            "/cache",
            StaticFiles(directory="cache"),
            name="cache",
        )

        # Mount static files（live2d-modelsはconfigからパス取得）
        frontend_config = getattr(config, "frontend", None)
        live2d_model_path = "live2d-models"
        # dict型の場合
        if isinstance(frontend_config, dict):
            live2d_model_path = frontend_config.get("live2d_model_path", live2d_model_path)
        # オブジェクト型の場合
        elif hasattr(frontend_config, "live2d_model_path"):
            live2d_model_path = frontend_config.live2d_model_path
        # 相対パスの場合は絶対パスに変換
        if not os.path.isabs(live2d_model_path):
            live2d_model_path = os.path.abspath(live2d_model_path)
        
        print(f"Live2D models directory: {live2d_model_path}")
        print(f"Directory exists: {os.path.exists(live2d_model_path)}")
        
        # カスタムエンドポイントの定義前にログ出力
        print("🚀 About to define custom Live2D endpoint...")
        
        # StaticFilesの代わりにカスタムエンドポイントを使用
        @self.app.get("/live2d-models/{file_path:path}")
        async def serve_live2d_file(file_path: str):
            """Live2Dファイルを提供するエンドポイント（パス正規化・詳細エラーログ付き）"""
            import mimetypes
            from fastapi.responses import FileResponse
            import logging
            logger = logging.getLogger("SecurityMiddleware")
            try:
                # パス正規化とディレクトリトラバーサル防止
                full_path = os.path.abspath(os.path.join(live2d_model_path, file_path))
                base_path = os.path.abspath(live2d_model_path)
                if not full_path.startswith(base_path):
                    msg403 = f"Directory traversal attempt: {file_path} -> {full_path}"
                    print(msg403)
                    logger.warning(msg403)
                    from fastapi import HTTPException
                    raise HTTPException(status_code=403, detail="Forbidden")
                # 必要最小限のリクエストログ
                msg = f"Live2D request: {file_path} -> {full_path}"
                print(msg)
                logger.info(msg)
                if os.path.exists(full_path) and os.path.isfile(full_path):
                    mime_type, _ = mimetypes.guess_type(full_path)
                    if mime_type is None:
                        if file_path.endswith('.json'):
                            mime_type = 'application/json'
                        elif file_path.endswith('.moc3'):
                            mime_type = 'application/octet-stream'
                        elif file_path.endswith('.png'):
                            mime_type = 'image/png'
                        else:
                            mime_type = 'application/octet-stream'
                    return FileResponse(
                        path=full_path,
                        media_type=mime_type,
                        headers={"Access-Control-Allow-Origin": "*"}
                    )
                else:
                    # 404時のみログ
                    msg404 = f"Live2D file not found: {full_path}"
                    print(msg404)
                    logger.warning(msg404)
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=404, 
                        detail=f"File not found: {full_path}"
                    )
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                msgerr = f"Live2D error: {str(e)} ({type(e).__name__})\n{tb}"
                print(msgerr)
                logger.error(msgerr)
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=500, 
                    detail=f"Internal server error: {str(e)}"
                )
        
        print(f"✅ Custom endpoint for /live2d-models -> {live2d_model_path}")
        print("🎯 Custom endpoint defined successfully!")

        self.app.mount(
            "/bg",
            StaticFiles(directory="backgrounds"),
            name="backgrounds",
        )
        self.app.mount(
            "/avatars",
            AvatarStaticFiles(directory="avatars"),
            name="avatars",
        )

        # Mount web tool directory separately from frontend
        self.app.mount(
            "/web-tool",
            CustomStaticFiles(directory="web_tool", html=True),
            name="web_tool",
        )

        # Mount main frontend with specific paths to avoid conflicts with WebSocket routes
        # Use a more specific path instead of root to avoid catching WebSocket routes
        @self.app.get("/")
        async def serve_frontend_root():
            """Serve the main frontend page"""
            from fastapi.responses import FileResponse
            return FileResponse("frontend/index.html")
        
        self.app.mount(
            "/assets",
            CustomStaticFiles(directory="frontend/assets"),
            name="frontend_assets",
        )
        
        # Mount libs directory for Live2D libraries
        self.app.mount(
            "/libs",
            CustomStaticFiles(directory="frontend/libs"),
            name="frontend_libs",
        )
        
        # Mount other frontend static files under /static
        self.app.mount(
            "/static",
            CustomStaticFiles(directory="frontend", html=True),
            name="frontend_static",
        )
        print("✅ Frontend mount re-enabled")

    def run(self):
        pass

    @staticmethod
    def clean_cache():
        """Clean the cache directory by removing and recreating it."""
        cache_dir = "cache"
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
            os.makedirs(cache_dir)
