import os
import sys
import re
import tempfile
from contextlib import nullcontext
from types import SimpleNamespace
import numpy as np

# Ensure top-level project modules are importable from any working directory.
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

from one_euro_filter import DeadbandOneEuroFilter2D
from calibration import GazeCalibration
from classifier import AcousticClassifier, AcousticFeatureExtractor
from voice_commander import VoiceCommander


def test_deadband_and_filter():
    print("\n========================================================")
    print(" 1. TESTING DEADBAND + ONE EURO FILTER DYNAMICS")
    print("========================================================")
    filt = DeadbandOneEuroFilter2D(deadband_radius=15.0, min_cutoff=0.8, beta=0.008)

    # Initial anchor position
    init_x, init_y = 500.0, 500.0
    s_x, s_y = filt.filter(init_x, init_y)
    assert abs(s_x - init_x) < 1e-3, "Initial filter output should match initial anchor"

    # Test micro-jitter inside 15px deadband (should remain pinned to anchor)
    print("[Deadband] Injecting micro-jitter within 12px radius...")
    jitter_outputs = []
    for _ in range(50):
        noisy_x = init_x + np.random.uniform(-10.0, 10.0)
        noisy_y = init_y + np.random.uniform(-10.0, 10.0)
        sx, sy = filt.filter(noisy_x, noisy_y)
        jitter_outputs.append((sx, sy))

    # Calculate variance inside deadband
    var_x = np.var([pt[0] for pt in jitter_outputs])
    var_y = np.var([pt[1] for pt in jitter_outputs])
    print(f" -> Output coordinate variance under jitter: X={var_x:.6f}, Y={var_y:.6f}")
    assert var_x < 1e-6 and var_y < 1e-6, "Deadband must completely freeze micro-jitter!"
    print(" -> SUCCESS: Deadband locks cursor when resting.")

    # Test deliberate sweep outside deadband with realistic 60fps timestamps (16.6ms per frame)
    print("[Filter] Simulating intentional head sweep (+200px displacement over 30 frames at 60fps)...")
    sweep_outputs = []
    sim_time = 1.0
    for step in range(1, 31):
        sim_time += 0.0166
        target_x = init_x + step * 7.0
        target_y = init_y + step * 5.0
        sx, sy = filt.filter(target_x, target_y, timestamp=sim_time)
        sweep_outputs.append((sx, sy))

    final_sx, final_sy = sweep_outputs[-1]
    print(f" -> Final smoothed coordinate: ({final_sx:.1f}, {final_sy:.1f})")
    assert final_sx > init_x + 100, "Filter should track intentional high-displacement motion"
    print(" -> SUCCESS: One Euro Filter transitions smoothly during movement.")


def test_calibration_lopo_cv():
    print("\n========================================================")
    print(" 2. TESTING 9-POINT GAZE CALIBRATION & LOPO-CV (HONEST ACCURACY)")
    print("========================================================")
    calib = GazeCalibration(screen_w=1920, screen_h=1080)

    # Simulate realistic user gaze data for 9 targets
    # Features: [yaw, pitch, iris_x, iris_y]
    # Ground truth mapping: screen_x ~ 960 + 40*yaw + 300*(iris_x-0.5)
    #                      screen_y ~ 540 + 35*pitch + 250*(iris_y-0.5)
    print("[Calibration] Simulating 30 frames for each of the 9 targets...")
    for tid, (nx, ny, ttype) in enumerate(calib.TARGET_GRID_9):
        target_px_x = nx * 1920
        target_px_y = ny * 1080

        # Invert linear mapping to generate plausible biometric angles
        base_yaw = (target_px_x - 960) / 45.0
        base_pitch = (target_px_y - 540) / 40.0
        base_iris_x = 0.5 + (target_px_x - 960) / 4000.0
        base_iris_y = 0.5 + (target_px_y - 540) / 3000.0

        frames = []
        for _ in range(30):
            # Add frame-to-frame sensor noise
            y_noise = base_yaw + np.random.normal(0, 0.4)
            p_noise = base_pitch + np.random.normal(0, 0.4)
            ix_noise = base_iris_x + np.random.normal(0, 0.01)
            iy_noise = base_iris_y + np.random.normal(0, 0.01)
            frames.append(np.array([y_noise, p_noise, ix_noise, iy_noise]))

        calib.add_target_samples(tid, frames)

    print(f"[Calibration] Total samples collected: {len(calib.samples)} across 9 targets.")
    print("[Calibration] Running Group-Aware Leave-One-Point-Out Cross Validation...")
    diag = calib.run_lopo_cross_validation()

    print("\n---------------- LOPO-CV DIAGNOSTIC REPORT ----------------")
    print(f" Best Ridge Alpha       : {diag['best_alpha']:.4f}")
    print(f" Center Error (1 point) : {diag['center_mean_px']:.2f} px (Interpolation)")
    print(f" Edge Error   (4 points): {diag['edge_mean_px']:.2f} px (Single-axis boundary)")
    print(f" Corner Error (4 points): {diag['corner_mean_px']:.2f} px (Extrapolation)")
    print(f" Overall Mean Error     : {diag['overall_mean_px']:.2f} px (Std: {diag['overall_std_px']:.2f} px)")
    print(" Target-by-Target Euclidean Errors (px):")
    for tid, err in diag["target_errors"].items():
        ttype = calib.TARGET_GRID_9[tid][2]
        print(f"   Target {tid} ({ttype:<6}): {err:.2f} px")
    print("-----------------------------------------------------------")

    # Corner extrapolation error should naturally be higher than center interpolation
    assert diag["overall_mean_px"] < 250.0, "Overall LOPO error should be well within usable range"
    print(" -> SUCCESS: Gaze calibration cross-validation pipeline validated.")


def test_audio_classifier():
    print("\n========================================================")
    print(" 3. TESTING AUDIO CLASSIFIER, DUAL NOISE GATE & LOO MATRIX")
    print("========================================================")
    clf = AcousticClassifier(sample_rate=16000, snr_multiplier=2.5, margin=0.6)

    # 1. Test Noise Floor Dual Mechanism
    print("[Audio DSP] Testing noise floor tracking and timeout latch...")
    # Baseline quiet
    for _ in range(20):
        clf.update_noise_floor(0.003)
    quiet_gate = clf.current_gate
    print(f" -> Quiet noise floor: {clf.noise_floor:.5f}, Gate: {quiet_gate:.5f}")

    # Sudden persistent noise (e.g. fan kicks in at RMS=0.03 for 60 frames)
    for i in range(60):
        is_above = clf.update_noise_floor(0.03)

    print(f" -> After 60 frames of persistent noise:")
    print(f"    Noise floor re-latched to: {clf.noise_floor:.5f}, Gate: {clf.current_gate:.5f}")
    assert clf.noise_floor > quiet_gate, "Timeout latch must update noise floor to adapt to persistent noise!"
    print(" -> SUCCESS: Dual-mechanism noise floor adapts to continuous noise without freezing.")

    # 2. Test Feature Extraction and Leave-One-Out Evaluation
    print("\n[Acoustic ML] Simulating calibration samples for 'Click' vs 'Supra-threshold Noise'...")
    sr = 16000
    click_samples = []
    # Generate 10 click samples (sharp high-frequency transient / damped sinusoid ~2500Hz)
    t = np.linspace(0, 0.08, int(sr * 0.08), endpoint=False)
    for _ in range(10):
        decay = np.exp(-t * 60)
        freq = 2400 + np.random.normal(0, 80)
        click = 0.8 * decay * np.sin(2 * np.pi * freq * t)
        noise = np.random.normal(0, 0.02, len(t))
        click_samples.append((click + noise).astype(np.float32))

    # Generate 10 noise samples (cough/speech: lower frequency rumble ~300Hz + broad energy)
    t_noise = np.linspace(0, 0.20, int(sr * 0.20), endpoint=False)
    noise_samples = []
    for _ in range(10):
        freq = 320 + np.random.normal(0, 40)
        low_rumble = 0.6 * np.sin(2 * np.pi * freq * t_noise)
        broadband = np.random.normal(0, 0.15, len(t_noise))
        noise_samples.append((low_rumble + broadband).astype(np.float32))

    calib_res = clf.calibrate(click_samples, noise_samples)
    loo = calib_res["loo_evaluation"]

    # Verify the user-confirmed "Oops, learn that" path updates only the
    # rolling dynamic bank. This harness keeps the classifier in memory.
    dynamic_before = len(clf.dynamic_clicks)
    clf.add_verified_sample(click_samples[0])
    assert len(clf.dynamic_clicks) == dynamic_before + 1, "Verified sample should enter the dynamic click bank"
    with tempfile.TemporaryDirectory(prefix="aura-learning-test-") as temp_dir:
        model_path = os.path.join(temp_dir, "acoustic_model.joblib")
        clf.save(model_path)
        restored = AcousticClassifier(sample_rate=16000)
        restored.load(model_path)
        assert len(restored.dynamic_clicks) == dynamic_before + 1, "Learned sample should persist across model save/load"
    print(f" -> Continuous learning bank: {dynamic_before} -> {len(clf.dynamic_clicks)} verified samples; save/load retained it")

    print("\n---------------- AUDIO LOO EVALUATION REPORT ----------------")
    print(f" Calibration Samples  : {calib_res['click_samples_count']} clicks, {calib_res['noise_samples_count']} noise")
    print(f" Data-Derived R_max   : {calib_res['r_max']:.3f} (Intra-click mean dist: {calib_res['mean_intra_click_dist']:.3f})")
    print(f" LOO Accuracy         : {loo['accuracy']*100:.1f}%")
    print(" Confusion Matrix [Row=True, Col=Pred]:")
    print(f"   [ TP (Click): {loo['tp']} | FN: {loo['fn']} ]")
    print(f"   [ FP        : {loo['fp']} | TN (Noise): {loo['tn']} ]")
    print("-------------------------------------------------------------")

    assert loo["accuracy"] >= 0.85, "Leave-one-out accuracy should be high on distinct acoustic clusters."

    # 3. Test Open-Set Outlier Rejection
    print("\n[Open-Set Rejection] Testing outlier sound (distant siren / foreign tone)...")
    t_outlier = np.linspace(0, 0.15, int(sr * 0.15), endpoint=False)
    outlier = (0.7 * np.sin(2 * np.pi * 9000 * t_outlier)).astype(np.float32)  # 9kHz ultrasonic screech
    is_click, conf, d_click = clf.classify_audio(outlier)
    print(f" -> Outlier classification: is_click={is_click}, dist_to_click={d_click:.2f}, R_max={calib_res['r_max']:.2f}")
    assert not is_click, "Outlier with distance > R_max must be rejected as Out-Of-Distribution!"
    print(" -> SUCCESS: Open-set outlier safely rejected by R_max threshold.")

def test_voice_command_guide():
    print("\n========================================================")
    print(" 4. TESTING THE 60 DOCUMENTED VOICE PHRASES")
    print("========================================================")

    class FakeKeyboard:
        def pressed(self, *keys):
            return nullcontext()
        def press(self, *keys):
            pass
        def release(self, *keys):
            pass
        def type(self, text):
            pass

    # Keep desktop operations inert while exercising the real parser.
    import voice_commander
    original_sys = voice_commander.sys
    voice_commander.sys = SimpleNamespace(platform="darwin")
    try:
        commander = object.__new__(VoiceCommander)
        commander.keyboard = FakeKeyboard()
        commander.is_listening = True
        commander.last_response = "Repeat response."
        commander.callback = None
        commander._send_ws = lambda message: None
        commander._speak_native = lambda text: False
        commander.scroll = lambda *args: None
        commander.trigger_zoom_in = lambda: None
        commander.trigger_zoom_out = lambda: None
        commander.trigger_spotlight = lambda *args: None
        commander.open_app = lambda app: f"Opening {app}"
        window_actions = []
        commander._window_action = lambda app, action: (window_actions.append((app, action)) or (True, app.title()))

        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        with open(html_path, encoding="utf-8") as html_file:
            guide = re.search(r"<summary>Voice command guide \(60 commands\)</summary><p>(.*?)</p>", html_file.read(), re.S)
        assert guide, "The visible 60-command guide should be present"
        phrases = [phrase.strip() for phrase in guide.group(1).split("·")]
        assert len(phrases) == 60, f"Expected 60 phrases, found {len(phrases)}"
        unsupported = []
        for phrase in phrases:
            utterance = phrase.replace("[phrase]", "cats").replace("[app]", "Safari")
            response = commander.execute_command(utterance)
            if response.startswith("I heard:"):
                unsupported.append(phrase)
        assert not unsupported, f"Unrecognized guide phrases: {unsupported}"
        for utterance, expected_action in (
            ("minimize Chrome app", "minimize"),
            ("maximize Safari app", "maximize"),
            ("close Chrome app", "close"),
        ):
            commander.execute_command(utterance)
            assert window_actions[-1] == (utterance.split()[1].lower(), expected_action), (
                f"Targeted app command was not routed correctly: {utterance} -> {window_actions[-1]}"
            )
        print(f" -> All {len(phrases)} phrases reached the command parser; OS actions were stubbed.")
    finally:
        voice_commander.sys = original_sys


if __name__ == "__main__":
    test_deadband_and_filter()
    test_calibration_lopo_cv()
    test_audio_classifier()
    test_voice_command_guide()
    print("\n========================================================")
    print(" ALL PORTION 1 DIAGNOSTICS & VERIFICATION TESTS PASSED! ")
    print("========================================================\n")
