"""AgentHost: backend-side agent loop for web candidate runtime.

One AgentHost instance per active web session. Drives the agent loop
(LLM call → tool dispatch → repeat) against a Toolset (LocalToolset first,
DockerSandboxToolset later). Events go through BackendEventSink → DB +
Broker → both candidate-web WS and interviewer WS via existing fan-out.
"""
