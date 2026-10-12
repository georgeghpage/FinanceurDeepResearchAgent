"""Run the real frontend script against the offline HTTP research server."""

import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from deepsearchagent.config import Config
from deepsearchagent.llm import KimiBackend
from deepsearchagent.models import ResearchPlan
from deepsearchagent.ui import ResearchHandler, ResearchServer
from deepsearchagent.orchestrator import SequentialResearchAgent
from test_agent import FakeSearch, TASK, draft, extraction, verification
from test_streaming import chunk


@unittest.skipUnless(shutil.which("node"), "Node.js is optional; install it to run frontend checks")
class WebTests(unittest.TestCase):
    def test_frontend_tracing_output_downloads_and_real_http_reconnection(self):
        release = threading.Event()
        backend = KimiBackend(Config(), "offline")
        self.addCleanup(backend.client.close)
        results = iter([ResearchPlan(objective="Find revenue", tasks=[TASK]),
                        extraction(), verification(), draft()])
        streams = []

        def create(**kwargs):
            result = next(results)

            def chunks():
                yield chunk(reasoning="Checking revenue sources.")
                if isinstance(result, ResearchPlan) and not release.wait(10):
                    raise RuntimeError("Frontend did not reconnect")
                yield chunk(content=result.model_dump_json(), finish="stop", tokens=100)

            stream = Mock()
            stream.__iter__ = Mock(return_value=chunks())
            streams.append(stream)
            return stream

        backend.client.chat.completions.create = create
        original_stream = ResearchHandler._stream

        def stream_after_reconnection(handler, run, after=0):
            if after:
                release.set()
            return original_stream(handler, run, after)

        server = ResearchServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("deepsearchagent.ui.ResearchAgent", SequentialResearchAgent), \
                    patch("deepsearchagent.ui.Config.from_env", return_value=Config()), \
                    patch("deepsearchagent.ui.require_api_key", return_value="offline"), \
                    patch("deepsearchagent.ui.require_tavily_key", return_value="offline"), \
                    patch("deepsearchagent.ui.AliyunBackend", return_value=backend) as factory, \
                    patch("deepsearchagent.ui.TavilyMCPSearch", return_value=FakeSearch()), \
                    patch.object(ResearchHandler, "_stream", stream_after_reconnection):
                env = {**os.environ, "RESEARCH_TEST_URL": f"http://127.0.0.1:{server.server_address[1]}"}
                result = subprocess.run([shutil.which("node"), "--test", str(Path(__file__).with_suffix(".cjs"))],
                                        env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                factory.assert_called_once()
                self.assertTrue(release.is_set(), "The real HTTP client never resumed its stream")
                self.assertEqual(backend.calls, 4)
                self.assertEqual(backend.tokens_used, 400)
                for stream in streams:
                    stream.close.assert_called_once()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()
