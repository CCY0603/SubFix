"""播放状态机 FollowFSM / 键位 Transport 的纯逻辑测试（虚拟时钟，不依赖 Resolve）。"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLLER = ROOT / ".subfix_support" / "subfix_playhead_poller.py"

spec = importlib.util.spec_from_file_location("subfix_playhead_poller", POLLER)
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

FollowFSM = poller.FollowFSM
Transport = poller.Transport
VK_SPACE, VK_J, VK_K, VK_L = poller.VK_SPACE, poller.VK_J, poller.VK_K, poller.VK_L
FPS = 25.0


class TransportTests(unittest.TestCase):
    def test_space_toggles_and_keeps_resume_direction(self):
        t = Transport()
        t.press(VK_SPACE)
        self.assertEqual(t.snapshot()[0], 1)
        t.press(VK_SPACE)
        self.assertEqual(t.snapshot()[0], 0)
        # 倒放暂停后再按空格，沿倒放方向恢复
        t.press(VK_J)
        self.assertEqual(t.snapshot()[0], -1)
        t.press(VK_SPACE)  # 暂停
        self.assertEqual(t.snapshot()[0], 0)
        t.press(VK_SPACE)  # 恢复 -> -1
        self.assertEqual(t.snapshot()[0], -1)

    def test_jkl_levels(self):
        t = Transport()
        for expect in (-1, -2, -4, -8):
            t.press(VK_J)
            self.assertEqual(t.snapshot()[0], expect)
        t.press(VK_L)  # 从倒放切正放先回 1
        self.assertEqual(t.snapshot()[0], 1)
        t.press(VK_L)
        self.assertEqual(t.snapshot()[0], 2)
        t.press(VK_K)
        self.assertEqual(t.snapshot()[0], 0)


class MousePlayHeuristicTests(unittest.TestCase):
    """鼠标/面板按钮播放：没有任何键事件，只有读取超时。"""

    def test_blocked_reads_arm_playback_and_fast_read_snaps_pause(self):
        fsm = FollowFSM()
        action, note = fsm.read_event("fast", 410, 100.0, 0, 1)
        self.assertEqual(note, "fast")
        self.assertIsNone(action)

        # 播放开始：读取在 GetCurrentTimecode 上卡死，被超时杀掉
        action, note = fsm.read_event("slow", None, 101.3, 0, 1)
        self.assertEqual(note, "heur-arm")
        self.assertTrue(fsm.eff_playing)
        self.assertEqual(fsm.eff_level, 1)
        self.assertEqual(fsm.anchor_rel, 410)
        self.assertEqual(fsm.anchor_wall, 100.0)

        rel, playing, speed, src = fsm.output(102.0, None, 0, FPS)
        self.assertTrue(playing)
        self.assertEqual(src, "heur")
        self.assertAlmostEqual(rel, 410 + 25 * 2.0)
        self.assertEqual(speed, 25.0)

        # 又一次超时，维持播放
        _, note = fsm.read_event("slow", None, 103.3, 0, 1)
        self.assertEqual(note, "slow")
        self.assertTrue(fsm.eff_playing)

        # 用户暂停：读取快速返回新帧 -> 立即判暂停并回贴
        action, note = fsm.read_event("fast", 500, 104.0, 0, 1)
        self.assertEqual(note, "fast-pause")
        self.assertEqual(action, "kill_reread")
        self.assertFalse(fsm.eff_playing)
        rel, playing, speed, src = fsm.output(104.1, 500, 0, FPS)
        self.assertFalse(playing)
        self.assertEqual(src, "real")
        self.assertEqual(rel, 500.0)

    def test_reverse_resume_direction_used_by_heuristic(self):
        fsm = FollowFSM()
        fsm.read_event("fast", 1000, 10.0, 0, -1)
        fsm.read_event("slow", None, 11.2, 0, -1)
        self.assertEqual(fsm.eff_level, -1)
        rel, playing, speed, src = fsm.output(12.0, None, 0, FPS)
        self.assertTrue(playing)
        self.assertEqual(src, "heur")
        self.assertAlmostEqual(rel, 1000 - 25 * 2.0)
        self.assertEqual(speed, -25.0)

    def test_slow_before_any_fast_read_does_not_arm(self):
        # 冷启动的首次超时可能只是 import 慢，不能当播放
        fsm = FollowFSM()
        _, note = fsm.read_event("slow", None, 1.2, 0, 1)
        self.assertEqual(note, "slow")
        self.assertFalse(fsm.eff_playing)
        _, note = fsm.read_event("fast", 100, 2.0, 0, 1)
        self.assertEqual(note, "fast")
        self.assertFalse(fsm.eff_playing)


class KeyboardPlayTests(unittest.TestCase):
    def test_keyboard_play_estimate_and_pause_snap(self):
        fsm = FollowFSM()
        fsm.read_event("fast", 410, 100.0, 0, 1)
        action, note = fsm.key_event(1, 1, "SP>1", 410, 100.0)
        self.assertEqual(note, "key-play")
        # 按键入播放时立刻丢弃暂停期已在途的读取，强制播放后重读
        self.assertEqual(action, "kill_reread")
        rel, playing, speed, src = fsm.output(101.0, 410, 1, FPS)
        self.assertTrue(playing)
        self.assertEqual(src, "key")
        self.assertAlmostEqual(rel, 435.0)

        # 播放期超时是常态，不改变档位
        fsm.read_event("slow", None, 101.3, 1, 1)
        self.assertTrue(fsm.eff_playing)
        self.assertEqual(fsm.eff_level, 1)

        # 空格暂停
        action, note = fsm.key_event(0, 1, "SP||", 410, 102.0)
        self.assertEqual(note, "key-pause")
        self.assertEqual(action, "kill_reread")
        self.assertFalse(fsm.eff_playing)
        # 首个真实帧回贴
        fsm.read_event("fast", 448, 102.2, 0, 1)
        rel, playing, _, src = fsm.output(102.3, 448, 0, FPS)
        self.assertFalse(playing)
        self.assertEqual(rel, 448.0)
        self.assertEqual(src, "real")

    def test_stale_inflight_read_right_after_key_must_not_pause(self):
        # 真机 trace 根因：按键前 spawn 的旧 oneshot 在按键后 265ms 带着旧帧快速返回，
        # 曾被误判 contradiction 把播放翻回暂停。宽限期内必须忽略。
        fsm = FollowFSM()
        fsm.read_event("fast", 1493, 38459.0, 0, 1)
        action, note = fsm.key_event(1, 1, "SP>1", 1493, 38459.147)
        self.assertEqual(note, "key-play")
        self.assertEqual(action, "kill_reread")
        # 265ms 后旧在途读取落地，帧完全没变
        _, note = fsm.read_event("fast", 1493, 38459.412, 1, 1)
        self.assertEqual(note, "fast-grace")
        self.assertTrue(fsm.eff_playing)
        # 输出仍按播放估算增长
        rel, playing, _, src = fsm.output(38459.5, 1493, 1, FPS)
        self.assertTrue(playing)
        self.assertEqual(src, "key")
        self.assertGreater(rel, 1493.0)

    def test_fast_read_during_key_play_with_moved_frame_is_pause_snap(self):
        # 宽限期外快速返回且帧已跳变：真实播放不可能，按暂停/跳转处理并回贴
        fsm = FollowFSM()
        fsm.read_event("fast", 410, 100.0, 0, 1)
        fsm.key_event(1, 1, "SP>1", 410, 100.0)
        _, note = fsm.read_event("fast", 410, 100.3, 1, 1)
        self.assertEqual(note, "fast-grace")
        action, note = fsm.read_event("fast", 600, 101.3, 1, 1)
        self.assertEqual(note, "fast-pause")
        self.assertEqual(action, "kill_reread")
        self.assertFalse(fsm.eff_playing)
        rel, _, _, src = fsm.output(101.4, 600, 0, FPS)
        self.assertEqual(rel, 600.0)
        self.assertEqual(src, "real")


class ContradictionTests(unittest.TestCase):
    def test_space_swallowed_by_edit_box_forces_pause(self):
        fsm = FollowFSM()
        fsm.read_event("fast", 410, 100.0, 0, 1)
        fsm.key_event(1, 1, "SP>1", 410, 100.0)
        # 宽限期内的静止帧忽略
        _, note = fsm.read_event("fast", 410, 100.5, 1, 1)
        self.assertEqual(note, "fast-grace")
        # 宽限期后第一次帧不动：只观察、立刻复读，不翻状态
        action, note = fsm.read_event("fast", 410, 101.2, 1, 1)
        self.assertEqual(note, "fast-watch")
        self.assertEqual(action, "kill_reread")
        self.assertTrue(fsm.eff_playing)
        # 复读仍然不动：空格确实被输入框吞掉，反证暂停
        action, note = fsm.read_event("fast", 411, 101.6, 1, 1)
        self.assertEqual(note, "contradiction")
        self.assertEqual(action, "kill_reread")
        self.assertFalse(fsm.eff_playing)
        rel, playing, _, _ = fsm.output(101.7, 411, 0, FPS)
        self.assertFalse(playing)
        self.assertEqual(rel, 411.0)

    def test_first_still_frame_then_blocked_read_clears_suspicion(self):
        # 第一次静止观察后，如果下一次读取阻塞（真在播），嫌疑应解除，不再 contradiction
        fsm = FollowFSM()
        fsm.read_event("fast", 410, 100.0, 0, 1)
        fsm.key_event(1, 1, "SP>1", 410, 100.0)
        fsm.read_event("fast", 410, 101.2, 1, 1)   # fast-watch
        _, note = fsm.read_event("slow", None, 102.4, 1, 1)
        self.assertEqual(note, "slow")
        self.assertTrue(fsm.eff_playing)
        # 嫌疑清零后再次单次静止，仍只是观察而非反证
        _, note = fsm.read_event("fast", 410, 103.6, 1, 1)
        self.assertEqual(note, "fast-watch")
        self.assertTrue(fsm.eff_playing)


class HeuristicAndKeyboardInteractionTests(unittest.TestCase):
    def test_space_during_heuristic_play_is_pause(self):
        fsm = FollowFSM()
        fsm.read_event("fast", 300, 50.0, 0, 1)
        fsm.read_event("slow", None, 51.2, 0, 1)
        self.assertTrue(fsm.eff_playing)
        # Transport 认为 level 0 -> 空格后 level=1（“开始”），但实际用户想暂停
        action, note = fsm.key_event(1, 1, "SP>1", None, 52.0)
        self.assertEqual(note, "heur-space-pause")
        self.assertEqual(action, "kill_reread")
        self.assertFalse(fsm.eff_playing)
        self.assertEqual(fsm.prev_key_level, 0)
        # 调用方随后把 transport 重置为 0；新的 key 快照不应再次触发播放
        _, note2 = fsm.key_event(0, 1, "SP||(heur)", None, 52.1)
        self.assertEqual(note2, "key-same")
        self.assertFalse(fsm.eff_playing)

    def test_keyboard_overrides_heuristic(self):
        fsm = FollowFSM()
        fsm.read_event("fast", 100, 0.0, 0, 1)
        fsm.read_event("slow", None, 1.2, 0, 1)
        self.assertEqual(fsm.eff_level, 1)
        # 用户按 L 进 2 倍速
        _, note = fsm.key_event(2, 1, "L2", None, 2.0)
        self.assertEqual(note, "key-play")
        self.assertEqual(fsm.eff_level, 2)
        rel, playing, speed, src = fsm.output(3.0, None, 2, FPS)
        self.assertTrue(playing)
        self.assertEqual(src, "key")
        self.assertAlmostEqual(rel, 100 + 50 * 1.0)

    def test_timeline_switch_resets_fsm(self):
        # 时间线切换在 main 里直接重建 FollowFSM；这里验证新实例行为干净
        fsm = FollowFSM()
        fsm.read_event("fast", 100, 0.0, 0, 1)
        fsm.read_event("slow", None, 1.2, 0, 1)
        self.assertTrue(fsm.eff_playing)
        fsm2 = FollowFSM()
        rel, playing, _, _ = fsm2.output(2.0, 200, 0, FPS)
        self.assertFalse(playing)
        self.assertEqual(rel, 200.0)


if __name__ == "__main__":
    unittest.main()
