"""Optional LivingMemory bridge without synthesizing incoming user messages."""


class LivingMemoryBridge:
    def __init__(self, plugin, event):
        self.plugin = plugin
        self.event = event

    async def compose_context(self, *, query, top_k=6, max_chars=5000, **_):
        return await self.plugin.autonomous_recall(
            self.event, query=query, top_k=top_k, max_chars=max_chars
        )

    async def record_visible_turn(self, *, role, content, **_):
        if role != "assistant":
            raise ValueError("Autonomous memory records must be assistant messages")
        await self.plugin.autonomous_record(self.event, content=content)
