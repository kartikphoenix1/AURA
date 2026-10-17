// Aura Frontend Application Logic
// Nintendo-tier audio, canvas cosmetics, 9-point calibration, sandboxes, and desktop buddy.

class AuraApp {
  constructor() {
    this.ws = null;
    this.wsUrl = "ws://127.0.0.1:8765";

    // Telemetry state
    this.cursorX = window.innerWidth / 2;
    this.cursorY = window.innerHeight / 2;
    this.prevCursorX = this.cursorX;
    this.prevCursorY = this.cursorY;
    this.isBlinkLocked = false;
    this.isAbortTriggered = false;

    // Dwell tracking for "Sniper" magnifier
    this.dwellAnchorX = this.cursorX;
    this.dwellAnchorY = this.cursorY;
    this.dwellStartTime = Date.now();
    this.isSniperActive = false;

    // Active cursor persona: 'ufo', 'wand', 'ink', 'ninja', 'zoomies'
    this.currentPersona = "ufo";
    this.trails = [];
    this.particles = [];
    this.tractorBeams = [];

    // Web Audio Synthesizer
    this.audioCtx = null;
    this.wasAtEdge = false;
    this.isSnoozing = false;

    // Calibration state
    this.calibGrid = [
      { x: 0.1, y: 0.1, label: "Top-Left (Corner)" },
      { x: 0.5, y: 0.1, label: "Top-Center (Edge)" },
      { x: 0.9, y: 0.1, label: "Top-Right (Corner)" },
      { x: 0.1, y: 0.5, label: "Mid-Left (Edge)" },
      { x: 0.5, y: 0.5, label: "Center (Interpolation)" },
      { x: 0.9, y: 0.5, label: "Mid-Right (Edge)" },
      { x: 0.1, y: 0.9, label: "Bottom-Left (Corner)" },
      { x: 0.5, y: 0.9, label: "Bottom-Center (Edge)" },
      { x: 0.9, y: 0.9, label: "Bottom-Right (Corner)" }
    ];
    this.currentCalibIndex = -1;

    // Sandbox & Easter eggs
    this.poppedBubbles = 0;
    this.totalBubbleClicks = 0;
    this.konamiSequence = [];
    this.lastEasterEggTime = 0;

    this.initCanvas();
    this.initDOM();
    this.initWebSocket();
    this.initBubbleGrid();
    this.animate();
  }

  // --- AUDIO SYNTHESIS (Nintendo Switch Style) ---
  initAudio() {
    if (!this.audioCtx) {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
  }

  playTone(freq, type = "sine", duration = 0.12, gainVal = 0.15) {
    try {
      this.initAudio();
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, this.audioCtx.currentTime);
      gain.gain.setValueAtTime(gainVal, this.audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.audioCtx.currentTime + duration);
      osc.connect(gain);
      gain.connect(this.audioCtx.destination);
      osc.start();
      osc.stop(this.audioCtx.currentTime + duration);
    } catch (e) {
      // Audio autoplay policy fallback
    }
  }

  playHoverSound() {
    this.playTone(520, "triangle", 0.06, 0.08); // Marimba blip
  }

  playClickSound() {
    // 2-tone ascending chime (Switch confirmation feel)
    this.playTone(523.25, "sine", 0.15, 0.2); // C5
    setTimeout(() => this.playTone(659.25, "sine", 0.18, 0.25), 60); // E5
    setTimeout(() => this.playTone(783.99, "sine", 0.22, 0.3), 120); // G5
  }

  playPopSound() {
    this.playTone(880, "sine", 0.08, 0.3);
  }

  playAppleChime() {
    this.playTone(659.25, "sine", 0.15, 0.25);
    setTimeout(() => this.playTone(880.0, "sine", 0.25, 0.3), 80);
  }

  playEdgeBloop() {
    this.playTone(240, "sine", 0.08, 0.12); // Gentle edge bump bloop
  }

  // --- CANVAS & PARTICLES ENGINE ---
  initCanvas() {
    this.canvas = document.getElementById("fx-canvas");
    this.ctx = this.canvas.getContext("2d");
    this.resizeCanvas();
    window.addEventListener("resize", () => this.resizeCanvas());
  }

  resizeCanvas() {
    this.canvas.width = window.innerWidth;
    this.canvas.height = window.innerHeight;
  }

  spawnClickRipples(x, y, count = 16) {
    const colors = ["#00f2fe", "#4facfe", "#2ecc71", "#ffa502", "#ff4757"];
    for (let i = 0; i < count; i++) {
      const angle = (Math.PI * 2 * i) / count + Math.random() * 0.2;
      const speed = 2 + Math.random() * 5;
      this.particles.push({
        x: x,
        y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        size: 3 + Math.random() * 4,
        color: colors[Math.floor(Math.random() * colors.length)],
        life: 1.0,
        decay: 0.03 + Math.random() * 0.02
      });
    }
  }

  // --- WEBSOCKET CONNECTION ---
  initWebSocket() {
    const statusPill = document.getElementById("status-connected");

    try {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = () => {
        statusPill.textContent = "WebSocket Online (60 FPS)";
        statusPill.className = "status-pill online";
      };

      this.ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "telemetry") {
          this.handleTelemetry(msg);
        } else if (msg.status) {
          this.handleServerResponse(msg);
        }
      };

      this.ws.onclose = () => {
        statusPill.textContent = "WebSocket Offline (Reconnecting...)";
        statusPill.className = "status-pill offline";
        if (!this._wsReconnecting) {
          this._wsReconnecting = true;
          setTimeout(() => {
            this._wsReconnecting = false;
            this.initWebSocket();
          }, 2000);
        }
      };

      this.ws.onerror = () => {
        this.ws.close();
      };
    } catch (e) {
      setTimeout(() => this.initWebSocket(), 2000);
    }
  }

  handleTelemetry(data) {
    this.cursorX = data.x;
    this.cursorY = data.y;
    this.isBlinkLocked = data.is_blink_locked;
    this.isAbortTriggered = data.abort_triggered;

    // Update Failsafe Pill
    const failsafePill = document.getElementById("status-failsafe");
    const buddyCap = document.getElementById("buddy-cap");
    const buddySpeech = document.getElementById("buddy-speech");

    // Update Camera status pill
    const cameraPill = document.getElementById("status-camera");
    const isDark = (typeof data.camera_brightness === "number" && data.camera_brightness < 5.0);

    if (data.camera_connected && data.face_detected) {
      cameraPill.textContent = "📷 Camera: Tracking Face";
      cameraPill.className = "status-pill tracking";
    } else if (data.camera_connected) {
      if (isDark) {
        cameraPill.textContent = "📷 Camera: Shutter Closed / Dark";
        cameraPill.className = "status-pill paused";
      } else {
        cameraPill.textContent = "📷 Camera: Scanning for Face...";
        cameraPill.className = "status-pill searching";
      }
    } else {
      cameraPill.textContent = "📷 Camera: Offline";
      cameraPill.className = "status-pill offline";
    }

    if (!data.camera_connected) {
      buddySpeech.textContent = "Camera disconnected!";
    } else if (isDark) {
      buddySpeech.textContent = "Webcam lens is covered or closed!";
    } else if (!data.face_detected) {
      buddySpeech.textContent = "Look into the camera!";
    } else if (this.isBlinkLocked) {
      failsafePill.textContent = "Long-Blink: Cursor Paused";
      failsafePill.className = "status-pill paused";
      buddyCap.classList.remove("hidden");
      buddySpeech.textContent = "Pippin is resting (Zzz)";
    } else if (this.isAbortTriggered) {
      failsafePill.textContent = "Extreme Pitch: Abort";
      failsafePill.className = "status-pill paused";
      buddySpeech.textContent = "Looking at ceiling!";
    } else {
      failsafePill.textContent = "Failsafe: Active";
      failsafePill.className = "status-pill normal";
      buddyCap.classList.add("hidden");
      buddySpeech.textContent = "Pippin is watching!";
    }

    // Update lock status span in diagnostics
    const lockStatus = document.getElementById("t-lock-status");
    if (lockStatus) {
      if (this.isBlinkLocked) {
        lockStatus.textContent = "LOCKED (Long Blink)";
        lockStatus.style.color = "var(--accent-amber)";
      } else if (this.isAbortTriggered) {
        lockStatus.textContent = "ABORTED (Extreme Pitch)";
        lockStatus.style.color = "var(--accent-red)";
      } else {
        lockStatus.textContent = "Active";
        lockStatus.style.color = "var(--accent-green)";
      }
    }

    // Diagnostics panel updates
    document.getElementById("t-x").textContent = Math.round(data.x);
    document.getElementById("t-y").textContent = Math.round(data.y);

    if (data.raw_pose) {
      document.getElementById("t-yaw").textContent = data.raw_pose.yaw.toFixed(1) + "°";
      document.getElementById("t-pitch").textContent = data.raw_pose.pitch.toFixed(1) + "°";
      document.getElementById("t-roll").textContent = data.raw_pose.roll.toFixed(1) + "°";
      const pitchPercent = Math.min(100, Math.max(0, (data.raw_pose.pitch / 25.0) * 100));
      document.getElementById("bar-pitch").style.width = pitchPercent + "%";
    }

    if (data.blendshapes) {
      document.getElementById("t-blink-l").textContent = data.blendshapes.blink_l.toFixed(2);
      document.getElementById("t-blink-r").textContent = data.blendshapes.blink_r.toFixed(2);
      document.getElementById("t-dyn-thresh").textContent = data.blendshapes.dynamic_blink_thresh.toFixed(2);
    }

    if (data.audio) {
      document.getElementById("t-rms").textContent = data.audio.rms.toFixed(3);
      document.getElementById("t-nfloor").textContent = data.audio.noise_floor.toFixed(3);
      const gateWidth = Math.min(100, (data.audio.rms / Math.max(data.audio.current_gate, 0.001)) * 50);
      document.getElementById("bar-rms").style.width = gateWidth + "%";
    }

    const clickTag = document.getElementById("t-click-intent");
    const confTag = document.getElementById("t-conf");
    clickTag.textContent = data.click ? "TRUE (CLICK)" : "False";
    clickTag.className = data.click ? "tag-click active" : "tag-click";
    confTag.textContent = Math.round((data.click_confidence || 0) * 100) + "%";

    // Check Screen Edge Bloop
    const isAtEdge = data.x <= 15 || data.x >= window.innerWidth - 15 || data.y <= 15 || data.y >= window.innerHeight - 15;
    if (isAtEdge && !this.wasAtEdge) {
      this.playEdgeBloop();
    }
    this.wasAtEdge = isAtEdge;

    // Handle Click Rising Edge
    if (data.click && !this.lastClickTelemetry) {
      this.triggerIntentClick(data.x, data.y);
    }
    this.lastClickTelemetry = data.click;

    // Check Easter Eggs (Konami eye code / zoomies / snooze)
    this.checkEasterEggs(data);
  }

  triggerIntentClick(x, y) {
    if (this.isSnoozing) {
      this.wakeUpFromSnooze();
      return;
    }

    this.playClickSound();
    this.spawnClickRipples(x, y);

    // Spawn UFO vertical tractor beam on click
    if (this.currentPersona === "ufo") {
      this.tractorBeams.push({ x: x, y: y, startTime: Date.now() });
    }

    // If currently calibrating, feed apple!
    if (this.currentCalibIndex >= 0) {
      this.advanceCalibrationTarget();
    }

    // If in Bubble wrap sandbox, test bubble pop
    const el = document.elementFromPoint(x, y);
    if (el && el.classList.contains("bubble-cell") && !el.classList.contains("popped")) {
      el.classList.add("popped");
      this.playPopSound();
      this.poppedBubbles++;
      this.totalBubbleClicks++;
      this.updateBubbleStats();
    } else if (el && el.classList.contains("theremin-note")) {
      this.playThereminNote(el.dataset.note);
      el.classList.add("active");
      setTimeout(() => el.classList.remove("active"), 250);
    }
  }

  // --- CALIBRATION LOGIC ---
  startCalibrationSequence() {
    this.currentCalibIndex = 0;
    document.querySelector(".calib-intro").classList.add("hidden");
    document.getElementById("calib-results-card").classList.add("hidden");
    this.showCurrentAppleTarget();
  }

  showCurrentAppleTarget() {
    const target = this.calibGrid[this.currentCalibIndex];
    const appleEl = document.getElementById("apple-target");
    appleEl.classList.remove("hidden");
    appleEl.style.left = (target.x * 100) + "vw";
    appleEl.style.top = (target.y * 100) + "vh";
    document.getElementById("target-label").textContent = `Target ${this.currentCalibIndex + 1}/9: ${target.label}`;

    // Signal Python to start recording frames for this target
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        command: "start_record_target",
        target_id: this.currentCalibIndex
      }));
    }
  }

  advanceCalibrationTarget() {
    this.playAppleChime();
    this.spawnClickRipples(this.cursorX, this.cursorY, 24);

    // Stop recording current target
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ command: "stop_record_target" }));
    }

    this.currentCalibIndex++;
    if (this.currentCalibIndex < this.calibGrid.length) {
      setTimeout(() => this.showCurrentAppleTarget(), 500);
    } else {
      // Completed all 9 targets! Run LOPO-CV
      document.getElementById("apple-target").classList.add("hidden");
      this.currentCalibIndex = -1;
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "finish_gaze_calibration" }));
      }
    }
  }

  handleServerResponse(msg) {
    if (msg.status === "gaze_calibration_finished") {
      const diag = msg.diagnostics;
      document.getElementById("val-overall-err").textContent = diag.overall_mean_px.toFixed(1) + " px";
      document.getElementById("val-center-err").textContent = diag.center_mean_px.toFixed(1) + " px";
      document.getElementById("val-edge-err").textContent = diag.edge_mean_px.toFixed(1) + " px";
      document.getElementById("val-corner-err").textContent = diag.corner_mean_px.toFixed(1) + " px";
      document.getElementById("calib-results-card").classList.remove("hidden");
    } else if (msg.status === "audio_calibration_finished") {
      alert(`Audio Calibrated! R_max: ${msg.evaluation.r_max.toFixed(2)}, LOO Accuracy: ${(msg.evaluation.loo_evaluation.accuracy * 100).toFixed(1)}%`);
      document.getElementById("audio-modal").classList.add("hidden");
    } else if (msg.status === "audio_click_recorded") {
      document.getElementById("count-click").textContent = msg.count;
    } else if (msg.status === "audio_noise_recorded") {
      document.getElementById("count-noise").textContent = msg.count;
    } else if (msg.status === "oops_learned") {
      this.playAppleChime();
      alert("Aura silently added the last 1.5s audio into the acoustic memory bank!");
    }
  }

  // --- BUBBLE WRAP SANDBOX ---
  initBubbleGrid() {
    const grid = document.getElementById("bubble-grid");
    grid.innerHTML = "";
    const bubbleColors = ["#00f2fe", "#4facfe", "#ff4757", "#2ecc71", "#ffa502", "#a55eea", "#ff6b81", "#70a1ff"];

    for (let i = 0; i < 32; i++) {
      const bubble = document.createElement("div");
      bubble.className = "bubble-cell";
      bubble.style.setProperty("--bubble-color", bubbleColors[i % bubbleColors.length]);
      bubble.addEventListener("mouseenter", () => this.playHoverSound());
      bubble.addEventListener("click", () => {
        if (!bubble.classList.contains("popped")) {
          bubble.classList.add("popped");
          this.playPopSound();
          this.spawnClickRipples(this.cursorX, this.cursorY, 12);
          this.poppedBubbles++;
          this.totalBubbleClicks++;
          this.updateBubbleStats();
        }
      });
      grid.appendChild(bubble);
    }
  }

  updateBubbleStats() {
    document.getElementById("stat-popped").textContent = this.poppedBubbles;
    const acc = this.totalBubbleClicks > 0 ? Math.round((this.poppedBubbles / this.totalBubbleClicks) * 100) : 100;
    document.getElementById("stat-accuracy").textContent = acc + "%";
  }

  resetBubbleGrid() {
    this.poppedBubbles = 0;
    this.totalBubbleClicks = 0;
    this.updateBubbleStats();
    document.querySelectorAll(".bubble-cell").forEach(b => b.classList.remove("popped"));
  }

  // --- EYE-THEREMIN PLAY MODE ---
  playThereminNote(noteName) {
    const noteFreqs = {
      C4: 261.63, D4: 293.66, E4: 329.63, F4: 349.23,
      G4: 392.00, A4: 440.00, B4: 493.88, C5: 523.25
    };
    const freq = noteFreqs[noteName] || 440.0;
    this.playTone(freq, "triangle", 0.4, 0.25);
  }

  // --- EASTER EGGS ---
  checkEasterEggs(data) {
    const now = Date.now();

    // 1. Konami Code for Eyes: Top-Left -> Top-Right -> Bottom-Left -> Bottom-Right
    const normX = this.cursorX / window.innerWidth;
    const normY = this.cursorY / window.innerHeight;
    let currentQuad = null;

    if (normX < 0.35 && normY < 0.35) currentQuad = "TL";
    else if (normX > 0.65 && normY < 0.35) currentQuad = "TR";
    else if (normX < 0.35 && normY > 0.65) currentQuad = "BL";
    else if (normX > 0.65 && normY > 0.65) currentQuad = "BR";

    if (currentQuad) {
      const lastQuad = this.konamiSequence[this.konamiSequence.length - 1];
      if (currentQuad !== lastQuad) {
        this.konamiSequence.push(currentQuad);
        if (this.konamiSequence.length > 4) this.konamiSequence.shift();

        // Check if matching TL -> TR -> BL -> BR
        if (this.konamiSequence.join(",") === "TL,TR,BL,BR") {
          this.currentPersona = "ninja";
          this.konamiSequence = [];
          this.playAppleChime();
          this.spawnClickRipples(this.cursorX, this.cursorY, 30);
          document.getElementById("buddy-speech").textContent = "Konami Code! Ninja Mode 🥷";
          setTimeout(() => {
            if (this.currentPersona === "ninja") this.currentPersona = "ufo";
          }, 8000);
        }
      }
    }

    if (now - this.lastEasterEggTime < 4000) return;

    // 2. Zoomies detection: rapid intentional head shaking
    if (data.raw_pose && Math.abs(data.raw_pose.yaw) > 28) {
      this.currentPersona = "zoomies";
      document.getElementById("buddy-speech").textContent = "ZOOMIES! 🏎️💨";
      this.lastEasterEggTime = now;
      setTimeout(() => {
        if (this.currentPersona === "zoomies") {
          this.currentPersona = "ufo";
          document.getElementById("buddy-speech").textContent = "Pippin is back!";
        }
      }, 3000);
    }

    // 3. Snooze Sound: sustained "Shhhhh" white-noise hiss
    if (data.audio && data.audio.rms > 0.04 && data.audio.is_above_gate && !data.click) {
      this.snoozeHissFrames = (this.snoozeHissFrames || 0) + 1;
      if (this.snoozeHissFrames > 45) { // ~1.5 seconds of sustained "Shhhh"
        this.triggerSnooze();
        this.snoozeHissFrames = 0;
        this.lastEasterEggTime = now;
      }
    } else {
      this.snoozeHissFrames = 0;
    }
  }

  triggerSnooze() {
    this.isSnoozing = true;
    document.getElementById("snooze-overlay").classList.add("active");
    document.getElementById("buddy-speech").textContent = "Shhhhh... Sleeping 💤";
  }

  wakeUpFromSnooze() {
    this.isSnoozing = false;
    document.getElementById("snooze-overlay").classList.remove("active");
    this.playAppleChime();
    document.getElementById("buddy-speech").textContent = "Awake & Ready!";
  }

  // --- DOM EVENT LISTENERS ---
  initDOM() {
    // Nav Tabs
    document.querySelectorAll(".nav-tab").forEach(tab => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".view-panel").forEach(p => p.classList.remove("active"));
        tab.classList.add("active");
        document.getElementById(`view-${tab.dataset.tab}`).classList.add("active");
        this.playHoverSound();
      });
    });

    // Persona Selector
    document.querySelectorAll(".persona-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".persona-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        this.currentPersona = btn.dataset.persona;
        this.playHoverSound();
      });
    });

    // Deadband Slider
    const deadbandSlider = document.getElementById("slider-deadband");
    deadbandSlider.addEventListener("input", (e) => {
      const val = e.target.value;
      document.getElementById("lbl-deadband").textContent = val + "px";
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "set_deadband", radius: parseFloat(val) }));
      }
    });

    // Start Calibration
    document.getElementById("btn-start-calibration").addEventListener("click", () => {
      this.startCalibrationSequence();
    });

    // Audio Setup Modal
    document.getElementById("btn-audio-setup").addEventListener("click", () => {
      document.getElementById("audio-modal").classList.remove("hidden");
    });
    document.getElementById("btn-close-audio").addEventListener("click", () => {
      document.getElementById("audio-modal").classList.add("hidden");
    });

    // Audio Record Actions
    document.getElementById("btn-rec-click").addEventListener("click", () => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "record_audio_click" }));
      }
    });
    document.getElementById("btn-rec-noise").addEventListener("click", () => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "record_audio_noise" }));
      }
    });
    document.getElementById("btn-finish-audio").addEventListener("click", () => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "finish_audio_calibration" }));
      }
    });

    // "Oops, learn that"
    document.getElementById("btn-oops-learn").addEventListener("click", () => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ command: "learn_oops" }));
      }
    });

    // Reset Bubbles
    document.getElementById("btn-reset-bubbles").addEventListener("click", () => {
      this.resetBubbleGrid();
      this.playClickSound();
    });

    // Goto bubbles after calibration
    document.getElementById("btn-goto-bubbles").addEventListener("click", () => {
      document.querySelector('[data-tab="bubblewrap"]').click();
    });

    // Wake up from snooze button
    const wakeBtn = document.getElementById("btn-wake-up");
    if (wakeBtn) {
      wakeBtn.addEventListener("click", () => this.wakeUpFromSnooze());
    }

    // Spacebar Emergency Kill Switch
    window.addEventListener("keydown", (e) => {
      if (e.code === "Space") {
        const killBtn = document.getElementById("btn-killswitch");
        killBtn.style.background = "#ff4757";
        killBtn.style.color = "#fff";
        setTimeout(() => {
          killBtn.style.background = "";
          killBtn.style.color = "";
        }, 300);
      }
    });
  }

  // --- ANIMATION LOOP (Custom Cursors, Sniper Magnifier & Buddy Eyes) ---
  animate() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    // 1. Dwell-to-Zoom "Sniper" Magnifier Logic
    const dwellDist = Math.hypot(this.cursorX - this.dwellAnchorX, this.cursorY - this.dwellAnchorY);
    const now = Date.now();

    if (dwellDist < 25) {
      if (now - this.dwellStartTime > 1200) {
        // Trigger Sniper Magnifier
        this.isSniperActive = true;
        const sniperEl = document.getElementById("sniper-bubble");
        sniperEl.classList.remove("hidden");
        sniperEl.style.left = this.cursorX + "px";
        sniperEl.style.top = this.cursorY + "px";
      }
    } else {
      this.dwellAnchorX = this.cursorX;
      this.dwellAnchorY = this.cursorY;
      this.dwellStartTime = now;
      if (this.isSniperActive) {
        this.isSniperActive = false;
        document.getElementById("sniper-bubble").classList.add("hidden");
      }
    }

    // 2. Add Cursor Trails based on Persona
    this.trails.push({ x: this.cursorX, y: this.cursorY, time: now });
    const maxTrailAge = this.currentPersona === "ink" ? 600 : 250;
    this.trails = this.trails.filter(t => now - t.time < maxTrailAge);

    // Render Trails
    if (this.currentPersona === "ufo") {
      // 1. Render Active Tractor Beams (conical beam on click)
      for (let i = this.tractorBeams.length - 1; i >= 0; i--) {
        const tb = this.tractorBeams[i];
        const elapsed = now - tb.startTime;
        if (elapsed > 400) {
          this.tractorBeams.splice(i, 1);
          continue;
        }
        const alpha = (1.0 - elapsed / 400) * 0.45;
        this.ctx.fillStyle = `rgba(0, 242, 254, ${alpha})`;
        this.ctx.beginPath();
        this.ctx.moveTo(tb.x, tb.y);
        this.ctx.lineTo(tb.x - 70, window.innerHeight);
        this.ctx.lineTo(tb.x + 70, window.innerHeight);
        this.ctx.closePath();
        this.ctx.fill();
      }

      // 2. Faint green UFO exhaust
      this.ctx.strokeStyle = "rgba(46, 204, 113, 0.4)";
      this.ctx.lineWidth = 3;
      this.ctx.beginPath();
      this.trails.forEach((t, i) => {
        if (i === 0) this.ctx.moveTo(t.x, t.y);
        else this.ctx.lineTo(t.x, t.y);
      });
      this.ctx.stroke();

      // Draw UFO Icon
      this.ctx.font = "24px sans-serif";
      this.ctx.textAlign = "center";
      this.ctx.textBaseline = "middle";
      this.ctx.fillText("🛸", this.cursorX, this.cursorY);

    } else if (this.currentPersona === "wand") {
      // Magic Wand Stardust trail
      this.ctx.fillStyle = "rgba(255, 215, 0, 0.8)";
      this.trails.forEach((t, i) => {
        const radius = (i / this.trails.length) * 4;
        this.ctx.beginPath();
        this.ctx.arc(t.x + (Math.random() - 0.5) * 6, t.y + (Math.random() - 0.5) * 6, radius, 0, Math.PI * 2);
        this.ctx.fill();
      });
      this.ctx.font = "22px sans-serif";
      this.ctx.fillText("🪄", this.cursorX, this.cursorY);

    } else if (this.currentPersona === "ink") {
      // Ink brush trajectory line
      this.ctx.strokeStyle = "rgba(0, 242, 254, 0.6)";
      this.ctx.lineWidth = 6;
      this.ctx.lineCap = "round";
      this.ctx.beginPath();
      this.trails.forEach((t, i) => {
        if (i === 0) this.ctx.moveTo(t.x, t.y);
        else this.ctx.lineTo(t.x, t.y);
      });
      this.ctx.stroke();

      this.ctx.font = "22px sans-serif";
      this.ctx.fillText("🖌️", this.cursorX, this.cursorY);

    } else if (this.currentPersona === "ninja") {
      // Ninja shadow smoke trail
      this.ctx.fillStyle = "rgba(100, 100, 120, 0.5)";
      this.trails.forEach((t, i) => {
        this.ctx.beginPath();
        this.ctx.arc(t.x, t.y, (i / this.trails.length) * 6, 0, Math.PI * 2);
        this.ctx.fill();
      });
      this.ctx.font = "24px sans-serif";
      this.ctx.fillText("🥷", this.cursorX, this.cursorY);

    } else if (this.currentPersona === "zoomies") {
      this.ctx.font = "28px sans-serif";
      this.ctx.fillText("🏎️", this.cursorX, this.cursorY);
    }

    // 3. Render Particles
    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.x += p.vx;
      p.y += p.vy;
      p.life -= p.decay;

      if (p.life <= 0) {
        this.particles.splice(i, 1);
        continue;
      }

      this.ctx.fillStyle = p.color;
      this.ctx.globalAlpha = p.life;
      this.ctx.beginPath();
      this.ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
      this.ctx.fill();
      this.ctx.globalAlpha = 1.0;
    }

    // 4. Update Desktop Buddy Mascot Eyes (tracks cursor)
    const buddyEl = document.getElementById("desktop-buddy");
    if (buddyEl) {
      const rect = buddyEl.getBoundingClientRect();
      const buddyCenterX = rect.left + rect.width / 2;
      const buddyCenterY = rect.top + rect.height / 2;
      const angle = Math.atan2(this.cursorY - buddyCenterY, this.cursorX - buddyCenterX);
      const pupilOffset = 3.5;
      const px = Math.cos(angle) * pupilOffset;
      const py = Math.sin(angle) * pupilOffset;

      document.getElementById("pupil-left").style.transform = `translate(${px}px, ${py}px)`;
      document.getElementById("pupil-right").style.transform = `translate(${px}px, ${py}px)`;
    }

    requestAnimationFrame(() => this.animate());
  }
}

// Boot application when DOM is ready
window.addEventListener("DOMContentLoaded", () => {
  window.auraApp = new AuraApp();
});
