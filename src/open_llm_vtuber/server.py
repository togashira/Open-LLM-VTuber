import time
import os
import shutil
import logging
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from fastapi.responses import JSONResponse
from .routes import init_client_ws_route, init_webtool_routes
from .service_context import ServiceContext
from .config_manager.utils import Config



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
        
        client_ip = request.client.host
        user_agent = request.headers.get("user-agent", "").lower()
        path = request.url.path
        
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
        import os
        print(f"[DEBUG] StaticFiles get_response: directory={self.directory}, path={path}")
        abs_path = os.path.join(self.directory, path)
        print(f"[DEBUG] StaticFiles resolved absolute path: {abs_path}")
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            print(f"[DEBUG] 404 Not Found (resolved): {abs_path}")
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

        async def not_found_handler(request: Request, exc):
            print(f"[DEBUG] 404 Not Found: {request.url.path}")
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
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

        # Live2Dモデルのロード状況をログ出力
        try:
            live2d_models = []
            frontend_config = getattr(config, "frontend", None)
            if frontend_config is not None:
                if isinstance(frontend_config, dict):
                    live2d_models = frontend_config.get("live2d_models", [])
                elif hasattr(frontend_config, "live2d_models"):
                    live2d_models = getattr(frontend_config, "live2d_models", [])
            print(f"[Live2D] frontend_config: {frontend_config}")
            print(f"[Live2D] live2d_models (raw): {live2d_models}")
            # live2d-modelsディレクトリの絶対パスを解決
            base_dir = frontend_config.get("base_dir", ".") if isinstance(frontend_config, dict) else getattr(frontend_config, "base_dir", ".")
            live2d_model_path = frontend_config.get("live2d_model_path", "live2d-models") if isinstance(frontend_config, dict) else getattr(frontend_config, "live2d_model_path", "live2d-models")
            live2d_model_dir = os.path.join(base_dir, live2d_model_path)
            live2d_model_dir = os.path.abspath(live2d_model_dir)
            print(f"[Live2D] live2d_model_dir: {live2d_model_dir}")
            print(f"[Live2D] Directory exists: {os.path.exists(live2d_model_dir)}")
            if os.path.exists(live2d_model_dir):
                print(f"[Live2D] live2d-modelsディレクトリ内: {os.listdir(live2d_model_dir)}")
            print(f"[DEBUG] frontend_config type: {type(frontend_config)} value: {frontend_config}")
            print(f"[DEBUG] live2d_models type: {type(live2d_models)} value: {live2d_models}")
            if isinstance(live2d_models, list):
                for m in live2d_models:
                    model_dir = os.path.join(live2d_model_dir, m.get('path', m.get('name', '')))
                    print(f"[Live2D] モデル '{m.get('name', m)}' のパス: {model_dir} 存在: {os.path.exists(model_dir)}")
                if len(live2d_models) > 1:
                    print(f"✅ Live2Dモデル複数取得OK: {[m.get('name', m) for m in live2d_models]}")
                elif len(live2d_models) == 1:
                    print(f"⚠️ Live2Dモデル単体のみ取得: 失敗扱い [{live2d_models[0].get('name', live2d_models[0])}]")
                else:
                    print("❌ Live2Dモデルが0件です")
            else:
                print("❌ live2d_modelsの型が不正です")
        except Exception as e:
            print(f"❌ Live2Dモデル取得時エラー: {e}")

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
        # base_dir, live2d_model_pathを取得
        if isinstance(frontend_config, dict):
            base_dir = frontend_config.get("base_dir", "")
            live2d_model_path = frontend_config.get("live2d_model_path", "live2d-models")
        elif hasattr(frontend_config, "base_dir"):
            base_dir = getattr(frontend_config, "base_dir", "")
            live2d_model_path = getattr(frontend_config, "live2d_model_path", "live2d-models")
        else:
            base_dir = "."
            live2d_model_path = "live2d-models"

        # base_dir + live2d_model_path でパスを合成
        live2d_model_dir = os.path.join(base_dir, live2d_model_path)
        try:
            # 追加デバッグ: config全体の型・内容をprint
            print(f"[DEBUG] config type: {type(config)}")
            try:
                import pprint
                pprint.pprint(config.__dict__ if hasattr(config, '__dict__') else config)
            except Exception as e:
                print(f"[DEBUG] config pprint error: {e}")

            live2d_models = []
            frontend_config = getattr(config, "frontend", None)
            print(f"[DEBUG] frontend_config type: {type(frontend_config)} value: {frontend_config}")
            if frontend_config is not None:
                if isinstance(frontend_config, dict):
                    live2d_models = frontend_config.get("live2d_models", [])
                elif hasattr(frontend_config, "live2d_models"):
                    live2d_models = getattr(frontend_config, "live2d_models", [])
            print(f"[DEBUG] live2d_models type: {type(live2d_models)} value: {live2d_models}")
            # live2d-modelsディレクトリの絶対パスを解決
            base_dir = frontend_config.get("base_dir", ".") if isinstance(frontend_config, dict) else getattr(frontend_config, "base_dir", ".")
            live2d_model_path = frontend_config.get("live2d_model_path", "live2d-models") if isinstance(frontend_config, dict) else getattr(frontend_config, "live2d_model_path", "live2d-models")
            live2d_model_dir = os.path.join(base_dir, live2d_model_path)
            live2d_model_dir = os.path.abspath(live2d_model_dir)
            print(f"[Live2D] live2d_model_dir: {live2d_model_dir}")
            print(f"[Live2D] Directory exists: {os.path.exists(live2d_model_dir)}")
            if os.path.exists(live2d_model_dir):
                print(f"[Live2D] live2d-modelsディレクトリ内: {os.listdir(live2d_model_dir)}")
            if isinstance(live2d_models, list):
                for m in live2d_models:
                    model_dir = os.path.join(live2d_model_dir, m.get('path', m.get('name', '')))
                    print(f"[Live2D] モデル '{m.get('name', m)}' のパス: {model_dir} 存在: {os.path.exists(model_dir)}")
                if len(live2d_models) > 1:
                    print(f"✅ Live2Dモデル複数取得OK: {[m.get('name', m) for m in live2d_models]}")
                elif len(live2d_models) == 1:
                    print(f"⚠️ Live2Dモデル単体のみ取得: 失敗扱い [{live2d_models[0].get('name', live2d_models[0])}]")
                else:
                    print("❌ Live2Dモデルが0件です")
            else:
                print("❌ live2d_modelsの型が不正です")
        except Exception as e:
            print(f"❌ Live2Dモデル取得時エラー: {e}")
                # ...不要なインデントエラー部分を削除...
        
        print(f"✅ Custom endpoint for /live2d-models -> {live2d_model_path}")
        print("🎯 Custom endpoint defined successfully!")

        # conf.yamlのbase_dir+backgrounds_pathから背景ディレクトリを取得
        # base_dir, backgrounds_path, avatars_path, assets_pathを一度だけ取得し、以降は変数を使い回す
        if isinstance(frontend_config, dict):
            base_dir = frontend_config.get("base_dir", "")
            backgrounds_path = frontend_config.get("backgrounds_path", "backgrounds")
            avatars_path = frontend_config.get("avatars_path", "avatars")
            assets_path = frontend_config.get("assets_path", "assets")
        elif hasattr(frontend_config, "base_dir"):
            base_dir = getattr(frontend_config, "base_dir", "")
            backgrounds_path = getattr(frontend_config, "backgrounds_path", "backgrounds")
            avatars_path = getattr(frontend_config, "avatars_path", "avatars")
            assets_path = getattr(frontend_config, "assets_path", "assets")
        else:
            base_dir = "."
            backgrounds_path = "backgrounds"
            avatars_path = "avatars"
            assets_path = "assets"

        bg_dir = os.path.join(base_dir, backgrounds_path)
        avatars_dir = os.path.join(base_dir, avatars_path)
        assets_dir = os.path.join(base_dir, assets_path)
        self.app.mount(
            "/bg",
            StaticFiles(directory=bg_dir),
            name="backgrounds",
        )
        # conf.yamlのbase_dir+avatars_pathからアバターディレクトリを取得
        avatars_dir = None
        if isinstance(frontend_config, dict):
            base_dir = frontend_config.get("base_dir", "")
            avatars_path = frontend_config.get("avatars_path", "avatars")
            avatars_dir = os.path.join(base_dir, avatars_path)
        elif hasattr(frontend_config, "base_dir") and hasattr(frontend_config, "avatars_path"):
            base_dir = getattr(frontend_config, "base_dir", "")
            avatars_path = getattr(frontend_config, "avatars_path", "avatars")
            avatars_dir = os.path.join(base_dir, avatars_path)
        else:
            avatars_dir = "avatars"
        self.app.mount(
            "/avatars",
            AvatarStaticFiles(directory=avatars_dir),
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
