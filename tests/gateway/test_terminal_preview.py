"""Terminal progress previews: sleep-aware mechanical preview lines.

``agent.display.terminal_preview_line`` must spend the preview budget on the work,
not on a leading ``sleep`` — and must not split command chains inside quotes: a
quoted SQL ``;`` orphaned the preview tail in production (the preview led with
``" 2>&1 | head -55`` instead of the command).
"""

from agent.display import terminal_preview_line, terminal_wait_badge


class TestTerminalPreviewLine:
    def test_sleep_chain_shows_work_not_wait(self):
        cmd = 'sleep 400; grep -c "done: cards" /tmp/lsh-fanout-run.log'
        out = terminal_preview_line(cmd, cap=40)
        assert out.startswith("⏳ ~7m → ")
        assert "grep -c" in out  # the budget went to the work
        assert "sleep" not in out.split("→", 1)[1]

    def test_pure_sleep_is_a_wait_line(self):
        assert terminal_preview_line("sleep 240", cap=40) == "⏳ waiting ~4m"

    def test_short_plain_command_passthrough(self):
        assert terminal_preview_line("gh pr view 278", cap=40) == "gh pr view 278"

    def test_long_plain_command_head_truncated(self):
        out = terminal_preview_line("git log --oneline " + "x" * 60, cap=40)
        assert len(out) == 40 and out.endswith("...")

    def test_multiline_first_line_plus_count(self):
        out = terminal_preview_line("echo start\npython build.py\necho done", cap=40)
        assert out == "echo start ...+2 lines"

    def test_chain_previews_last_non_sleep_segment(self):
        out = terminal_preview_line("sleep 30 && npm test && npm run build", cap=60)
        assert "npm run build" in out

    def test_quoted_sql_semicolon_is_not_a_chain_split(self):
        cmd = ('echo "=== ids ==="; sqlite3 "file:db" '
               '"SELECT id FROM tasks ORDER BY id;" 2>&1 | head -55')
        out = terminal_preview_line(cmd, cap=64)
        # The in-SQL semicolon must not orphan the tail: the preview leads with the
        # command, not with the post-quote redirect (`" 2>&1 | head -55`).
        assert out.startswith("sqlite3") and len(out) <= 64

    def test_quoted_sql_concat_operator_is_not_a_chain_split(self):
        cmd = 'psql -c "SELECT a || b FROM t;"'
        assert terminal_preview_line(cmd, cap=64) == cmd

    def test_wait_badge_survives_quoted_semicolons(self):
        assert terminal_wait_badge('sleep 30; sqlite3 "db" "SELECT 1;"') == "⏳ 30s → "

    def test_wait_badge_sums_all_sleeps(self):
        assert terminal_wait_badge("sleep 60 && run.sh && sleep 60") == "⏳ ~2m → "

    def test_wait_badge_ignores_short_waits(self):
        assert terminal_wait_badge("run.sh") == ""