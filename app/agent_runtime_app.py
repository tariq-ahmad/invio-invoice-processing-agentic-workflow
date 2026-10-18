# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Agent Runtime (Vertex AI Agent Engine) entrypoint.

agents-cli introspects `agent_runtime` from this module to deploy to Agent
Runtime. This is a deploy-only wrapper: it reuses the SAME resumable App
(root_agent graph + resumability config) that the Cloud Run service runs, so
the evaluated agent matches production. The Cloud-Run-only concerns (React UI,
AgentMail WebSocket intake, SSE) live in app.fast_api_app and are intentionally not
part of this runtime — Agent Runtime only serves the agent's query interface,
which is what the managed evaluation service invokes.
"""

from vertexai.agent_engines.templates.adk import AdkApp

from app.agent import app as _resumable_app

agent_runtime = AdkApp(app=_resumable_app, enable_tracing=True)
