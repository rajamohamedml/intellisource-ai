"""Shared pytest fixtures and sample data.

Every fixture here exists to keep the test suite fully offline: no test
in this package makes a real network or Anthropic API call.
`fake_anthropic_client` stands in for the real SDK client wherever
`chunker.py` needs `.messages.count_tokens`; the SAMPLE_* Java snippets
let `java_parser.py` be tested without cloning a repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _FakeCountTokensResponse:
    input_tokens: int


class _FakeMessages:
    """Stands in for `anthropic.Anthropic().messages` -- only the one
    method `chunker.py` actually calls is implemented.
    """

    def count_tokens(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> _FakeCountTokensResponse:
        text = messages[0]["content"]
        # A simple, deterministic stand-in for a real tokenizer -- good
        # enough to exercise batching thresholds without a network call.
        # Tool definitions count toward input tokens, as they do for real.
        tool_text = json.dumps(tools) + json.dumps(tool_choice) if tools else ""
        return _FakeCountTokensResponse(input_tokens=max(1, len(text) // 4) + len(tool_text) // 4)


class FakeAnthropicClient:
    """A minimal stand-in for `anthropic.Anthropic`, exposing only
    `.messages.count_tokens`, which is all `chunker.py` needs from it.
    """

    def __init__(self) -> None:
        self.messages = _FakeMessages()


@pytest.fixture
def fake_anthropic_client() -> FakeAnthropicClient:
    return FakeAnthropicClient()


SAMPLE_CONTROLLER_JAVA = '''package com.example.app.services.catalog.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Exposes read endpoints for actors.
 */
@RestController
@RequestMapping("/actors")
public class ActorController {

    /**
     * Look up one actor by id.
     */
    @GetMapping("/{id}")
    public ActorDto getActor(Integer id) {
        if (id == null) {
            throw new IllegalArgumentException("id required");
        }
        for (int i = 0; i < 3; i++) {
            if (i == id) {
                return new ActorDto();
            }
        }
        return null;
    }
}
'''

SAMPLE_NESTED_DTO_JAVA = '''package com.example.app.services.catalog.domain.dto;

public class ActorDto {

    public static class Actor {
        private Integer actorId;

        public Integer getActorId() {
            return actorId;
        }
    }

    public static class ActorRequest {
        private String firstName;

        public String getFirstName() {
            return firstName;
        }
    }
}
'''

SAMPLE_BROKEN_JAVA = "public class {{{ this is not valid java"
