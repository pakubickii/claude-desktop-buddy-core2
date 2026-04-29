#pragma once
#include <stdint.h>

// Hardcoded quote pool for the buddy speech-bubble feature. One-liners,
// kept short (≤56 chars) so they word-wrap to ≤3 lines inside the
// ~140 px-wide right-half bubble at font size 1. Personality sits at the
// intersection of sassy / geeky / technical — the same tone we want
// the optional LLM-generated quotes (sent from buddy_relay) to match,
// which is why this list also doubles as a few-shot reference in the
// LLM system prompt.
//
// Adding a quote: append before QUOTE_COUNT. No re-ordering needed —
// idle picker uses random() and avoids picking the same one twice in a
// row, that's all.

static const char* const QUOTES[] = {
  "rebooting > rewriting",
  "it works on my machine",
  "regex did nothing wrong",
  "shipped beats perfect",
  "git blame is a love language",
  "tabs vs spaces is a culture war",
  "your tests passed because they were easy",
  "merge conflicts build character",
  "documentation: write-only memory",
  "TODO: write better TODOs",
  "i am the test environment",
  "it's not a bug, it's emergent behavior",
  "stack overflow is my therapist",
  "off-by-one is 99% of bugs",
  "the cloud is just somebody's computer",
  "127.0.0.1 — there's no place like home",
  "code never lies, comments sometimes do",
  "production is the new staging",
  "every commit message is a love letter",
  "monday is just a side effect",
  "ssh into my heart",
  "404: motivation not found",
  "compile times are mandatory breaks",
  "you can't grep your way out of bad arch",
  "i'm not lazy, i'm async",
  "have you considered: less microservices?",
  "the database is fine. it's always the db.",
  "containerize me harder",
  "if it builds, it ships",
  "sleep is just garbage collection",
  "i've seen things... in production",
  "ctrl+c, ctrl+v: senior engineering",
  "wifi works in mysterious ways",
  "self documenting code is a myth",
  "yaml is just json with feelings",
  "rm -rf ~/regrets",
  "kubectl explain pain",
  "your linter is mad at you again",
  "the only good cache is no cache",
  "DNS. it was always DNS.",
  "have you tried turning it off and on?",
  "your terminal is showing",
  "schrodinger's bug: works until observed",
  "nobody reads the README",
  "everything is a tradeoff. except YAML.",
};

static const uint16_t QUOTE_COUNT = sizeof(QUOTES) / sizeof(QUOTES[0]);
