from collections import defaultdict

from loguru import logger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime
import time
import requests
from typing import Dict, List, Optional

class YouTubeCommentAdapter:
    def __init__(self, api_key: str, channel_id: str):
        self.api_key = api_key
        self.channel_id = channel_id
        self.youtube = build("youtube", "v3", developerKey=self.api_key)
        self.video_id = self.get_latest_video_id()
        self.live_chat_id = self.get_live_chat_id() if self.video_id else None
        self.next_page_token = None

    def get_latest_video_id(self):
        try:
            response = self.youtube.search().list(
                part="id",
                channelId=self.channel_id,
                eventType="live",
                type="video",
                order="date",
                maxResults=1
            ).execute()

            items = response.get("items", [])
            if not items:
                logger.warning("❌ 現在ライブ配信中の動画が見つかりません。")
                return None

            video_id = items[0]["id"]["videoId"]
            logger.info(f"✅ ライブ動画ID取得成功: {video_id}")
            return video_id

        except HttpError as e:
            logger.error(f"❌ ライブ動画ID取得エラー: {e}")
            return None

    def get_live_chat_id(self):
        try:
            response = self.youtube.videos().list(
                part="liveStreamingDetails",
                id=self.video_id
            ).execute()

            items = response.get("items", [])
            if not items:
                logger.warning("❌ ライブ配信詳細が見つかりません。")
                return None

            live_chat_id = items[0]["liveStreamingDetails"].get("activeLiveChatId")
            logger.info(f"✅ live_chat_id 取得成功: {live_chat_id}")
            return live_chat_id

        except HttpError as e:
            logger.error(f"❌ live_chat_id 取得エラー: {e}")
            return None

    def get_new_comments(self):
        if not self.live_chat_id:
            return []

        try:
            response = self.youtube.liveChatMessages().list(
                liveChatId=self.live_chat_id,
                part="snippet,authorDetails",
                pageToken=self.next_page_token,
                maxResults=10
            ).execute()

            self.next_page_token = response.get("nextPageToken")

            comments = []
            for item in response.get("items", []):
                message = item["snippet"].get("displayMessage")
                author = item["authorDetails"].get("displayName")
                comment_id = item["id"]
                comments.append({
                    "id": comment_id,
                    "author": author,
                    "message": message
                })

            return comments

        except Exception as e:
            logger.error(f"❌ ライブチャット取得エラー: {e}")
            return []

class GroupTalk:
    def __init__(self):
        self.comment_history: Dict[str, Dict] = {}

    def add_comment(self, comment: Dict):
        comment_id = comment["id"]
        if comment_id not in self.comment_history:
            self.comment_history[comment_id] = comment
            return True
        return False

    def get_all_comments(self) -> List[Dict]:
        return list(self.comment_history.values())

class YouTubeGroupTalk(GroupTalk):
    def __init__(self, yt_adapter: YouTubeCommentAdapter):
        super().__init__()
        self.yt_adapter = yt_adapter
        self._seen_comment_ids = set()
        self._author_comment_counts = defaultdict(int)  # 投稿者名→コメント回数

    def poll_comments(self):
        comments = self.yt_adapter.get_new_comments()
        new_comments = []
        for comment in comments:
            author = comment.get("author", "YouTubeUser")
            self._author_comment_counts[author] += 1
            count = self._author_comment_counts[author]
            # コメント内容に「また来てくれてありがとう」や「常連」フラグを付与
            if count >= 3:
                comment["regular_label"] = "常連"
            elif count == 2:
                comment["regular_label"] = "また来てくれてありがとう"
            else:
                comment["regular_label"] = ""
            comment_id = comment["id"]
            if comment_id not in self._seen_comment_ids:
                self._seen_comment_ids.add(comment_id)
                if self.add_comment(comment):
                    new_comments.append(comment)
        return new_comments

    def process_comments(self):
        new_comments = self.poll_comments()
        for comment in new_comments:
            author = comment["author"]
            message = comment["message"]
            print(f"[コメント] {author}: {message}")
            payload = {"user_input": f"{author} さんのコメント: {message}"}
            try:
                res = requests.post("http://localhost:12393/api/chat", json=payload)
                ai_response = res.json().get("ai_response")
                print(f"AI応答: {author}: {ai_response}")
            except Exception as api_err:
                print(f"API送信エラー: {api_err}")

if __name__ == "__main__":
    import sys
    from os import getenv
    from dotenv import load_dotenv

    load_dotenv()
    api_key = getenv("YOUTUBE_API_KEY")
    channel_id = getenv("YOUTUBE_CHANNEL_ID")

    yt_adapter = YouTubeCommentAdapter(api_key=api_key, channel_id=channel_id)
    if not yt_adapter.live_chat_id:
        print("❌ live_chat_id取得失敗。配信が無いかコメント無効")
        sys.exit(1)

    print(f"✅ live_chat_id: {yt_adapter.live_chat_id}")
    group_talk = YouTubeGroupTalk(yt_adapter)

    while True:
        try:
            group_talk.process_comments()
        except Exception as e:
            print(f"💥 Error: {e}")
        time.sleep(5)
