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
    Different releases expose different helper sets, so every helper is bound
    defensively; ``usable`` reports whether the minimum set is present.
    """

    def __init__(self, scope_module, utils_module):
        """Bind the helpers LivingMemory's own tools call, tolerating old versions."""
        self.adapter_access = getattr(scope_module, "adapter_access", None)
        self.resolve_event_memory_access = getattr(
            scope_module, "resolve_event_memory_access", None
        )
        self.is_event_memory_allowed = getattr(
            scope_module, "is_event_memory_allowed", None
        )
        self.resolve_memory_scope = getattr(scope_module, "resolve_memory_scope", None)
        self.get_persona_id = getattr(utils_module, "get_persona_id", None)

    @property
    def usable(self) -> bool:
        """Report whether the helpers the bridge requires are all callable."""
        return all(
            callable(item)
            for item in (
                self.adapter_access,
                self.is_event_memory_allowed,
                self.resolve_memory_scope,
            )
        )


class LivingMemoryNativeBridge:
    """Drive LivingMemory's own services when it exposes no autonomous_* hooks.

    The third-party plugin is never edited. Recall reuses the exact path its own
    agent tool uses: ``adapter_access`` -> ``resolve_event_memory_access`` ->
    ``memory_engine.search_memories``. Writes reuse its memorize tool path:
    ``build_memory_from_structured_data`` -> ``add_memory``. Both therefore obey
    LivingMemory's whitelist, scope and persona filtering settings. Older
    releases lack some of these entry points; the bridge degrades to the
    equivalents their own tools use instead of raising.
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

    async def _persona_id(self, context):
        """Resolve the persona id, falling back to the event's own value.

        Args:
            context: LivingMemory plugin context.

        Returns:
            The persona id LivingMemory should filter by, or the event's value.
        """
        getter = getattr(self.scopes, "get_persona_id", None)
        if callable(getter):
            try:
                return await getter(context, self.event)
            except Exception:
                pass
        return getattr(self.event, "persona_id", None)

    async def _recall_target(self, config, context):
        """Resolve (allowed, scope, persona) with whichever API this version has.

        Newer releases expose one combined ``resolve_event_memory_access``;
        older ones only the separate whitelist and scope helpers their memorize
        tool uses. Both paths end in the same LivingMemory filtering rules.

        Args:
            config: LivingMemory ConfigManager.
            context: LivingMemory plugin context.

        Returns:
            (allowed, scope_id, persona_id); allowed=False when filtered out.
        """
        scopes = self.scopes
        if callable(scopes.resolve_event_memory_access):
            access = await scopes.resolve_event_memory_access(
                config, context, self.event
            )
            if not getattr(access, "allowed", False):
                return False, None, None
            return True, access.scope, access.persona_id
        if not scopes.is_event_memory_allowed(config, self.event):
            return False, None, None
        scope_id = scopes.resolve_memory_scope(config, self.event)
        return True, scope_id, await self._persona_id(context)

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
            allowed, scope_id, persona_id = await self._recall_target(config, context)
            if not allowed:
                return ""
            scope_id = scope_id or self.event.unified_msg_origin
            try:
                memories = await engine.search_memories(
                    query=query,
                    k=limit,
                    session_id=scope_id,
                    persona_id=persona_id,
                )
            except TypeError:
                # 旧版引擎的检索签名没有 persona_id 参数。
                memories = await engine.search_memories(
                    query=query, k=limit, session_id=scope_id
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
        config = getattr(self.plugin, "config_manager", None)
        context = getattr(self.plugin, "context", None)
        if not text or engine is None or config is None or context is None:
            return
        with self.scopes.adapter_access(config, self.event, write=True):
            if not self.scopes.is_event_memory_allowed(config, self.event):
                return
            session_id = self.event.unified_msg_origin
            memory_scope = (
                self.scopes.resolve_memory_scope(config, self.event) or session_id
            )
            persona_id = await self._persona_id(context)
            summary = text[:600]
            build = getattr(self.processor, "build_memory_from_structured_data", None)
            if callable(build):
                built, metadata, importance = build(
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
            else:
                # 旧版没有结构化构建入口：直接存原文，标记来源即可。
                built, metadata, importance = summary, {}, 0.5
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["source_window"] = {
                "session_id": session_id,
                "triggered_by": "autonomous_companion",
            }
            metadata["memory_origin"] = "autonomous_companion"
            metadata["source_session_id"] = session_id
            try:
                await engine.add_memory(
                    content=built,
                    session_id=memory_scope,
                    persona_id=persona_id,
                    importance=importance,
                    metadata=metadata,
                )
            except TypeError:
                # 旧版引擎的写入签名没有 persona_id 参数。
                await engine.add_memory(
                    content=built,
                    session_id=memory_scope,
                    importance=importance,
                    metadata=metadata,
                )
