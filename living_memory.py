"""Optional LivingMemory bridges without synthesizing incoming user messages."""


class LivingMemoryBridge:
    """Preferred bridge: LivingMemory's dedicated autonomous_* hooks."""

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


class LivingMemoryScopes:
    """Bundle of LivingMemory scope helpers, borrowed from its agent tools.

    Imported from the third-party package at runtime so this plugin keeps
    working whether or not LivingMemory is installed, enabled or updated.
    """

    def __init__(self, scope_module, utils_module):
        """Bind the exact helpers LivingMemory's own tools call."""
        self.adapter_access = scope_module.adapter_access
        self.resolve_event_memory_access = scope_module.resolve_event_memory_access
        self.is_event_memory_allowed = scope_module.is_event_memory_allowed
        self.resolve_memory_scope = scope_module.resolve_memory_scope
        self.get_persona_id = utils_module.get_persona_id


class LivingMemoryNativeBridge:
    """Drive LivingMemory's own services when it exposes no autonomous_* hooks.

    The third-party plugin is never edited. Recall reuses the exact path its own
    agent tool uses: ``adapter_access`` -> ``resolve_event_memory_access`` ->
    ``memory_engine.search_memories``. Writes reuse its memorize tool path:
    ``build_memory_from_structured_data`` -> ``add_memory``. Both therefore obey
    LivingMemory's whitelist, scope and persona filtering settings.
    """

    def __init__(self, plugin, event, scopes):
        """Store the LivingMemory plugin, its event, and its scope helpers."""
        self.plugin = plugin
        self.event = event
        self.scopes = scopes

    @property
    def engine(self):
        """LivingMemory's memory engine, or None when the plugin is not ready."""
        return getattr(getattr(self.plugin, "initializer", None), "memory_engine", None)

    @property
    def processor(self):
        """LivingMemory's memory processor, or None when the plugin is not ready."""
        return getattr(
            getattr(self.plugin, "initializer", None), "memory_processor", None
        )

    async def compose_context(self, *, query, top_k=6, max_chars=5000, **_):
        """Recall scoped memories and render them as one prompt block.

        Args:
            query: Recall keywords.
            top_k: Maximum number of memories to ask for.
            max_chars: Hard cap on rendered characters.

        Returns:
            Bullet lines, or an empty string when memory is unavailable.
        """
        query = " ".join(str(query or "").split())
        engine = self.engine
        config = getattr(self.plugin, "config_manager", None)
        context = getattr(self.plugin, "context", None)
        if not query or engine is None or config is None or context is None:
            return ""
        try:
            limit = max(1, min(int(top_k or 6), 20))
        except (TypeError, ValueError):
            limit = 6
        with self.scopes.adapter_access(config, self.event):
            access = await self.scopes.resolve_event_memory_access(
                config, context, self.event
            )
            if not access.allowed:
                return ""
            memories = await engine.search_memories(
                query=query,
                k=limit,
                session_id=access.scope,
                persona_id=access.persona_id,
            )
        lines = []
        used = 0
        for memory in memories or []:
            text = " ".join(str(getattr(memory, "content", "") or "").split())
            if not text:
                continue
            if used + len(text) > max_chars:
                break
            used += len(text)
            lines.append(f"- {text}")
        return "\n".join(lines)

    def _is_group_chat(self) -> bool:
        """Report whether the current event is a group message.

        LivingMemory's own tool reads ``event.get_message_type()``; when that is
        unavailable we fall back to the unified message origin layout
        (``platform:MessageType:session``).

        Returns:
            True for group conversations.
        """
        getter = getattr(self.event, "get_message_type", None)
        if callable(getter):
            try:
                kind = getter()
                kind = getattr(kind, "value", kind)
                return "group" in str(kind).lower()
            except Exception:
                pass
        return ":GroupMessage:" in str(
            getattr(self.event, "unified_msg_origin", "")
        )

    async def record_visible_turn(self, *, role, content, **_):
        """Persist one autonomous assistant message into LivingMemory.

        Args:
            role: Only assistant turns are recorded; anything else is an error.
            content: Visible text that was actually sent.

        Raises:
            ValueError: When asked to record a non-assistant turn.
        """
        if role != "assistant":
            raise ValueError("Autonomous memory records must be assistant messages")
        text = " ".join(str(content or "").split())
        engine = self.engine
        processor = self.processor
        config = getattr(self.plugin, "config_manager", None)
        context = getattr(self.plugin, "context", None)
        if not text or engine is None or processor is None or config is None:
            return
        if context is None:
            return
        with self.scopes.adapter_access(config, self.event, write=True):
            if not self.scopes.is_event_memory_allowed(config, self.event):
                return
            session_id = self.event.unified_msg_origin
            memory_scope = (
                self.scopes.resolve_memory_scope(config, self.event) or session_id
            )
            persona_id = await self.scopes.get_persona_id(context, self.event)
            summary = text[:600]
            built, metadata, importance = processor.build_memory_from_structured_data(
                structured_data={
                    "summary": summary,
                    "topics": [],
                    "key_facts": [],
                    "sentiment": "neutral",
                    "importance": None,
                },
                is_group_chat=self._is_group_chat(),
                fallback_excerpt=summary,
            )
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["source_window"] = {
                "session_id": session_id,
                "triggered_by": "autonomous_companion",
            }
            metadata["memory_origin"] = "autonomous_companion"
            metadata["source_session_id"] = session_id
            await engine.add_memory(
                content=built,
                session_id=memory_scope,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
            )
