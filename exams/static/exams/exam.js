/* SecureCode Arena exam environment (Stages 3 and 4).
 *
 * This script only DISPLAYS what the server decides. The server owns the deadline,
 * counts violations, ends the exam, evaluates code (through an external runner) and
 * calculates scores. A browser cannot be fully locked down: the monitored events are
 * indicators for review, and a user can disable or bypass them.
 *
 * Everything received from the server is inserted with textContent, never as HTML.
 */
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("exam-config").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    gate: $("exam-gate"), gateTitle: $("gate-title"), gateText: $("gate-text"),
    gateButton: $("gate-button"), gateError: $("gate-error"),
    layout: $("exam-layout"), list: $("exam-question-list"), problem: $("exam-problem"),
    code: $("exam-code"), saveStatus: $("save-status"), timer: $("exam-timer"),
    warnings: $("exam-warnings"), banner: $("exam-banner"), submit: $("exam-submit"),
    language: $("exam-language"), btnRun: $("btn-run"), btnSolution: $("btn-submit-solution"),
    results: $("exam-results"), runnerNote: $("runner-note"), hint: $("editor-hint")
  };

  var state = cfg.state;
  var questions = [];
  var languages = [];
  var runnerConfigured = false;
  var lastLanguage = "";
  var current = -1;
  var remainingAtSync = state.seconds_remaining;
  var syncedAt = performance.now();
  var running = false;
  var ended = false;
  var submitting = false;
  var evaluating = false;
  var dirty = {};
  var saveTimer = null;
  var tickTimer = null;
  var syncTimer = null;
  var bannerTimer = null;
  var confirmTimer = null;
  var solutionConfirmTimer = null;
  var lastReport = {};

  // ---------------------------------------------------------------- network
  function request(url, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: { "X-CSRFToken": cfg.csrfToken, "X-Requested-With": "fetch" }
    };
    if (options.fields) {
      var body = new FormData();
      Object.keys(options.fields).forEach(function (k) { body.append(k, options.fields[k]); });
      init.body = body;
    }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  function fail(r) {
    var message = (r.data && r.data.message) || "Something went wrong. Please try again.";
    if (r.status === 401) { message = "You have been signed out. Sign in again to continue."; }
    var error = new Error(message);
    error.response = r;
    return error;
  }

  // ---------------------------------------------------------------- code editor (CodeMirror if available)
  var editor = (function () {
    var cm = null;
    if (window.CodeMirror) {
      try {
        cm = window.CodeMirror.fromTextArea(el.code, {
          lineNumbers: true, indentUnit: 4, tabSize: 4, indentWithTabs: false,
          matchBrackets: true, autoCloseBrackets: true, styleActiveLine: true, theme: "arena",
          extraKeys: {
            Tab: function (c) { if (c.somethingSelected()) { c.indentSelection("add"); } else { c.replaceSelection("    ", "end"); } },
            "Shift-Tab": "indentLess"
          }
        });
      } catch (e) { cm = null; }
    }
    return {
      get: function () { return cm ? cm.getValue() : el.code.value; },
      set: function (v) { if (cm) { cm.setValue(v); } else { el.code.value = v; } },
      setMode: function (m) { if (cm) { cm.setOption("mode", m || "text/plain"); } },
      onChange: function (fn) {
        if (cm) { cm.on("change", function (c, change) { if (change.origin !== "setValue") { fn(); } }); }
        else { el.code.addEventListener("input", fn); }
      },
      refresh: function () { if (cm) { cm.refresh(); } },
      isRich: !!cm
    };
  })();

  // ---------------------------------------------------------------- fullscreen
  function fsElement() { return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function supportsFullscreen() {
    var root = document.documentElement;
    return !!(root.requestFullscreen || root.webkitRequestFullscreen);
  }
  function requestFullscreen() {
    var root = document.documentElement;
    var fn = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!fn) { return Promise.reject(new Error("Your browser does not support fullscreen mode.")); }
    var result = fn.call(root);
    return result && result.then ? result : Promise.resolve();
  }
  function exitFullscreen() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen;
    if (fn && fsElement()) {
      try { var p = fn.call(document); if (p && p.catch) { p.catch(function () {}); } } catch (e) { /* ignore */ }
    }
  }

  // ---------------------------------------------------------------- gate overlay
  function showGate(mode) {
    el.layout.hidden = true;
    el.gate.hidden = false;
    el.gateError.hidden = true;
    el.gateButton.hidden = false;
    if (mode === "begin") {
      el.gateTitle.textContent = "Enter fullscreen to begin";
      el.gateText.textContent = "Your timer starts when you enter fullscreen and begin. Leaving fullscreen, switching tabs or windows, or trying to leave this page will be recorded.";
      el.gateButton.textContent = "Enter fullscreen and begin";
    } else if (mode === "reenter") {
      el.gateTitle.textContent = "Return to fullscreen";
      el.gateText.textContent = "Your timer is still running. Re-enter fullscreen to continue.";
      el.gateButton.textContent = "Return to fullscreen";
    } else {
      el.gateTitle.textContent = "Fullscreen is not available";
      el.gateText.textContent = "This exam needs a browser that supports fullscreen mode. Please use a current desktop browser such as Chrome, Edge or Firefox.";
      el.gateButton.hidden = true;
    }
  }
  function gateError(message) {
    el.gateError.textContent = message;
    el.gateError.hidden = false;
  }
  function showExam() {
    el.gate.hidden = true;
    el.layout.hidden = false;
    editor.refresh();
  }

  // ---------------------------------------------------------------- state from the server
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function formatTime(total) {
    var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return (h ? h + ":" + pad(m) : pad(m)) + ":" + pad(s);
  }
  function remaining() {
    return Math.max(0, remainingAtSync - (performance.now() - syncedAt) / 1000);
  }

  function applyState(next) {
    if (!next) { return; }
    var previous = state.violations_counted;
    state = next;
    remainingAtSync = state.seconds_remaining;
    syncedAt = performance.now();
    updateWarnings();
    if (state.violations_counted > previous && !state.ended) { showWarning(); }
    if (state.ended) { finish(); }
  }

  function updateWarnings() {
    var n = state.violations_counted, max = state.max_violations;
    if (n > 0 || max) {
      el.warnings.hidden = false;
      el.warnings.textContent = "EVENTS " + n + (max ? "/" + max : "");
      el.warnings.classList.toggle("is-hot", !!max && n >= max - 1 && n > 0);
    }
  }

  function showBanner(text) {
    el.banner.textContent = text;
    el.banner.hidden = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(function () { el.banner.hidden = true; }, 12000);
  }
  function showWarning() {
    var n = state.violations_counted, max = state.max_violations;
    var text = "Warning: a browser event was recorded (" + n + (max ? " of " + max : "") + "). ";
    text += max ? "Your exam is submitted automatically at " + max + "." : "The organizers may review it.";
    showBanner(text);
  }

  function syncState() {
    return request(cfg.urls.state).then(function (r) {
      if (r.ok) { applyState(r.data.state); }
      else if (r.data && r.data.state) { applyState(r.data.state); }
    }).catch(function () { /* offline: the server will notice the gap */ });
  }

  // ---------------------------------------------------------------- timer
  function tick() {
    if (state.status !== "in_progress") { return; }
    var r = Math.ceil(remaining());
    el.timer.textContent = formatTime(r);
    el.timer.classList.toggle("is-low", r <= 300);
    if (r <= 0 && !submitting) { submitExam(true); }
  }

  // ---------------------------------------------------------------- violations (indicators only)
  function report(kind, detail) {
    if (ended || !running) { return; }
    var now = Date.now();
    if (lastReport[kind] && now - lastReport[kind] < 1000) { return; }
    lastReport[kind] = now;
    request(cfg.urls.violation, { method: "POST", fields: { kind: kind, detail: detail } })
      .then(function (r) { if (r.data && r.data.state) { applyState(r.data.state); } })
      .catch(function () {});
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") { report("tab_hidden", "Tab or window hidden"); }
  }
  function onBlur() { report("focus_lost", "Window lost focus"); }
  function onFullscreenChange() {
    if (!fsElement() && running && !ended) {
      report("fullscreen_exit", "Left fullscreen");
      flushSaves();
      showGate("reenter");
    }
  }
  function onBeforeUnload(event) {
    if (ended) { return; }
    try {
      var data = new FormData();
      data.append("kind", "navigation_attempt");
      data.append("detail", "Tried to leave or reload the page");
      data.append("csrfmiddlewaretoken", cfg.csrfToken);
      navigator.sendBeacon(cfg.urls.violation, data);
    } catch (e) { /* ignore */ }
    event.preventDefault();
    event.returnValue = "";
  }

  function startRunning() {
    if (running) { return; }
    running = true;
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", onBlur);
    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    window.addEventListener("beforeunload", onBeforeUnload);
    tickTimer = setInterval(tick, 250);
    syncTimer = setInterval(function () { syncState(); flushSaves(); }, cfg.syncSeconds * 1000);
    el.submit.disabled = false;
    tick();
  }

  // ---------------------------------------------------------------- helpers to build DOM safely
  function el_(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function tabMeta(q) {
    var text = q.difficulty + " / " + q.marks + " marks";
    return q.summary ? text + " / " + q.summary : text;
  }

  // ---------------------------------------------------------------- questions list and problem panel
  function renderList() {
    el.list.textContent = "";
    questions.forEach(function (q, i) {
      var li = document.createElement("li");
      var button = el_("button", "q-tab");
      button.type = "button";
      button.appendChild(el_("span", "q-tab-title", (i + 1) + ". " + q.title));
      q.metaEl = el_("span", "q-tab-meta mono", tabMeta(q));
      button.appendChild(q.metaEl);
      button.addEventListener("click", function () { select(i); });
      li.appendChild(button);
      el.list.appendChild(li);
    });
  }

  function section(parent, label, text, asCode) {
    if (!text) { return; }
    parent.appendChild(el_("h3", "mono", label));
    parent.appendChild(el_(asCode ? "pre" : "p", asCode ? "sample" : "", text));
  }

  function languageBySlug(slug) {
    for (var i = 0; i < languages.length; i++) { if (languages[i].slug === slug) { return languages[i]; } }
    return null;
  }
  function starterFor(slug) { var l = languageBySlug(slug); return l ? l.starter : ""; }
  function applyMode(slug) { var l = languageBySlug(slug); editor.setMode(l ? l.mode : "text/plain"); }

  function select(index) {
    if (index === current || !questions[index]) { return; }
    saveCurrent();
    current = index;
    var q = questions[index];
    Array.prototype.forEach.call(el.list.querySelectorAll(".q-tab"), function (b, i) {
      if (i === index) { b.setAttribute("aria-current", "true"); } else { b.removeAttribute("aria-current"); }
    });
    el.problem.textContent = "";
    el.problem.appendChild(el_("h2", "", q.title));
    var meta = el_("div", "problem-meta");
    meta.appendChild(el_("span", "difficulty", q.difficulty));
    meta.appendChild(el_("span", "mono", q.marks + (q.marks === 1 ? " MARK" : " MARKS")));
    el.problem.appendChild(meta);
    section(el.problem, "PROBLEM", q.problem_statement);
    section(el.problem, "INPUT FORMAT", q.input_format);
    section(el.problem, "OUTPUT FORMAT", q.output_format);
    section(el.problem, "CONSTRAINTS", q.constraints);
    section(el.problem, "SAMPLE INPUT", q.sample_input, true);
    section(el.problem, "SAMPLE OUTPUT", q.sample_output, true);

    if (!q.language || !languageBySlug(q.language)) { q.language = (languageBySlug(lastLanguage) ? lastLanguage : (languages[0] ? languages[0].slug : "")); }
    el.language.value = q.language;
    applyMode(q.language);
    editor.set(q.code || starterFor(q.language));
    editor.refresh();
    el.saveStatus.textContent = q.code ? "Saved" : "Not saved yet";
    renderResultsFor(q);
  }

  // ---------------------------------------------------------------- saving drafts
  // Templates are built by the server with a placeholder id of 0: .../0/, .../0/run/ or .../0/submit/
  function urlFor(template, id) {
    return template.replace(/\/0\/(run\/|submit\/)?$/, function (match, tail) { return "/" + id + "/" + (tail || ""); });
  }
  function answerUrl(id) { return urlFor(cfg.urls.answerTemplate, id); }

  function saveQuestion(index) {
    var q = questions[index];
    if (!q || !dirty[q.id]) { return Promise.resolve(); }
    var sent = q.code || "";
    var sentLanguage = q.language || "";
    return request(answerUrl(q.id), { method: "POST", fields: { code: sent, language: sentLanguage } }).then(function (r) {
      if (r.ok) {
        if ((q.code || "") === sent && (q.language || "") === sentLanguage) { delete dirty[q.id]; }
        if (index === current) { el.saveStatus.textContent = "Saved " + new Date().toLocaleTimeString(); }
        applyState(r.data.state);
      } else {
        if (r.data && r.data.state) { applyState(r.data.state); }
        if (index === current) { el.saveStatus.textContent = (r.data && r.data.message) || "Save failed"; }
      }
    }).catch(function () {
      if (index === current) { el.saveStatus.textContent = "Save failed. Will retry."; }
    });
  }
  function saveCurrent() { return current >= 0 ? saveQuestion(current) : Promise.resolve(); }
  function flushSaves() {
    return Promise.all(questions.map(function (q, i) { return saveQuestion(i); }));
  }
  function markDirty() {
    if (current < 0) { return; }
    dirty[questions[current].id] = true;
    el.saveStatus.textContent = "Unsaved changes...";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveCurrent, 1200);
  }

  editor.onChange(function () {
    if (current < 0) { return; }
    questions[current].code = editor.get();
    markDirty();
  });

  el.language.addEventListener("change", function () {
    if (current < 0) { return; }
    var q = questions[current];
    var text = editor.get();
    var untouched = text.trim() === "" || text === starterFor(q.language);
    q.language = el.language.value;
    lastLanguage = q.language;
    applyMode(q.language);
    if (untouched) { editor.set(starterFor(q.language)); }
    markDirty();
  });

  // ---------------------------------------------------------------- judging: run and submit
  function loadJudging() {
    return request(cfg.urls.judging).then(function (r) {
      if (!r.ok) { throw fail(r); }
      languages = r.data.languages || [];
      runnerConfigured = !!r.data.runner_configured;
      el.language.textContent = "";
      languages.forEach(function (l) {
        var option = document.createElement("option");
        option.value = l.slug;
        option.textContent = l.name;
        el.language.appendChild(option);
      });
      el.language.disabled = languages.length === 0;
      questions.forEach(function (q) {
        var info = r.data.questions[q.id];
        if (info && info.best) { q.summary = info.best.passed + "/" + info.best.total + " tests"; }
      });
      if (!languages.length) {
        showRunnerNote("No programming language is enabled for this competition. You can still write notes in the editor; nothing can be run or scored.");
      } else if (!runnerConfigured) {
        showRunnerNote("No code runner is configured on this server, so code cannot be run or scored. Your code is still saved. Please tell the organizers.");
      }
      setControls();
    }).catch(function (error) {
      showRunnerNote("Could not load the code runner information: " + error.message);
    });
  }

  function showRunnerNote(text) { el.runnerNote.textContent = text; el.runnerNote.hidden = false; }

  function setControls() {
    var ready = languages.length > 0 && runnerConfigured && !ended;
    el.btnRun.disabled = !ready || evaluating;
    el.btnSolution.disabled = !ready || evaluating;
  }

  function renderPending(kind) {
    el.results.textContent = "";
    el.results.appendChild(el_("p", "result-pending", kind === "run"
      ? "Running your code against the public tests..." : "Submitting. Evaluating against all tests..."));
  }

  function renderSubmission(sub) {
    el.results.textContent = "";
    var head = el_("div", "result-head");
    head.appendChild(el_("span", "verdict verdict-" + (sub.verdict || sub.status), sub.verdict_label || sub.status_label));
    if (sub.status === "completed") { head.appendChild(el_("span", "mono", sub.passed + "/" + sub.total + " TESTS PASSED")); }
    head.appendChild(el_("span", "mono result-kind", sub.kind === "submit" ? "SUBMITTED" : "PRACTICE RUN"));
    el.results.appendChild(head);
    if (sub.message) { el.results.appendChild(el_("p", "result-message", sub.message)); }
    if (!sub.finished) { el.results.appendChild(el_("p", "result-pending", "Still evaluating. You can keep working; check Submission history after the exam.")); }
    if (sub.compile_output) {
      el.results.appendChild(el_("h3", "mono", "COMPILER OUTPUT"));
      el.results.appendChild(el_("pre", "sample", sub.compile_output));
    }
    var list = el_("ul", "case-list");
    sub.cases.forEach(function (c) {
      var item = el_("li", "case case-" + c.status);
      if (c.public) {
        var details = document.createElement("details");
        var summary = document.createElement("summary");
        summary.appendChild(el_("span", "", "Test " + c.index + " (public)"));
        summary.appendChild(el_("span", "verdict verdict-" + c.status, c.status_label));
        details.appendChild(summary);
        var body = el_("div", "case-body");
        [["INPUT", c.input], ["EXPECTED OUTPUT", c.expected], ["YOUR OUTPUT", c.output], ["ERROR OUTPUT", c.error]].forEach(function (pair) {
          if (pair[1] || pair[0] !== "ERROR OUTPUT") {
            body.appendChild(el_("h3", "mono", pair[0]));
            body.appendChild(el_("pre", "sample", pair[1] || ""));
          }
        });
        details.appendChild(body);
        item.appendChild(details);
      } else {
        var row = el_("div", "case-row");
        row.appendChild(el_("span", "", "Test " + c.index + " (private)"));
        row.appendChild(el_("span", "verdict verdict-" + c.status, c.status_label));
        item.appendChild(row);
      }
      list.appendChild(item);
    });
    el.results.appendChild(list);
    if (sub.kind === "submit" && sub.status === "completed") {
      el.results.appendChild(el_("p", "detail-note", "Your score is calculated by the server and shown after the organizers publish results."));
    }
  }

  function renderResultsFor(q) {
    el.results.textContent = "";
    if (q.lastSubmission) { renderSubmission(q.lastSubmission); }
  }

  function poll(sub, q) {
    return new Promise(function (resolve, reject) {
      var started = Date.now();
      (function loop() {
        if (sub.finished || Date.now() - started > 90000) { resolve(sub); return; }
        setTimeout(function () {
          request(urlFor(cfg.urls.submissionTemplate, sub.id)).then(function (r) {
            if (!r.ok) { reject(fail(r)); return; }
            applyState(r.data.state);
            sub = r.data.submission;
            loop();
          }).catch(reject);
        }, 1000);
      })();
    });
  }

  function evaluate(kind) {
    if (evaluating || ended || current < 0) { return; }
    var q = questions[current];
    var language = el.language.value;
    if (!language) { return; }
    evaluating = true;
    setControls();
    renderPending(kind);
    var template = kind === "run" ? cfg.urls.runTemplate : cfg.urls.submitTemplate;
    saveCurrent().then(function () {
      return request(urlFor(template, q.id), { method: "POST", fields: { language: language, code: editor.get() } });
    }).then(function (r) {
      if (!r.ok) { if (r.data && r.data.state) { applyState(r.data.state); } throw fail(r); }
      applyState(r.data.state);
      return poll(r.data.submission, q);
    }).then(function (sub) {
      q.lastSubmission = sub;
      if (kind === "submit" && sub.status === "completed") { q.summary = sub.passed + "/" + sub.total + " tests"; q.metaEl.textContent = tabMeta(q); }
      if (current >= 0 && questions[current] === q) { renderSubmission(sub); }
    }).catch(function (error) {
      el.results.textContent = "";
      el.results.appendChild(el_("p", "result-message", error.message));
    }).then(function () {
      evaluating = false;
      setControls();
    });
  }

  el.btnRun.addEventListener("click", function () { evaluate("run"); });
  el.btnSolution.addEventListener("click", function () {
    if (el.btnSolution.dataset.confirm === "1") {
      clearTimeout(solutionConfirmTimer);
      el.btnSolution.dataset.confirm = "";
      el.btnSolution.textContent = "Submit solution";
      evaluate("submit");
      return;
    }
    // In-page confirmation (a browser dialog could knock the page out of fullscreen).
    el.btnSolution.dataset.confirm = "1";
    el.btnSolution.textContent = "Click again to submit for scoring";
    solutionConfirmTimer = setTimeout(function () {
      el.btnSolution.dataset.confirm = "";
      el.btnSolution.textContent = "Submit solution";
    }, 5000);
  });

  // ---------------------------------------------------------------- load questions
  function loadQuestions() {
    if (questions.length) { return Promise.resolve(); }
    return request(cfg.urls.questions).then(function (r) {
      if (!r.ok) { throw fail(r); }
      questions = r.data.questions;
      applyState(r.data.state);
      return loadJudging();
    }).then(function () {
      renderList();
      select(0);
    });
  }

  // ---------------------------------------------------------------- submit exam and finish
  function finish() {
    if (ended) { return; }
    ended = true;
    clearInterval(tickTimer);
    clearInterval(syncTimer);
    window.removeEventListener("beforeunload", onBeforeUnload);
    exitFullscreen();
    // The server renders the final result page.
    window.location.assign(cfg.examUrl);
  }

  function submitExam(automatic) {
    if (submitting || ended) { return; }
    submitting = true;
    el.submit.disabled = true;
    flushSaves().then(function () {
      return request(cfg.urls.submit, { method: "POST", fields: {} });
    }).then(function (r) {
      if (r.data && r.data.state) { applyState(r.data.state); }
      finish();
    }).catch(function () {
      // Could not reach the server. The server will close the exam at the deadline anyway.
      submitting = false;
      el.submit.disabled = false;
      if (automatic) { setTimeout(function () { submitExam(true); }, 2000); }
      else { showBanner("Could not submit. Check your connection and try again."); }
    });
  }

  el.submit.addEventListener("click", function () {
    if (el.submit.dataset.confirm === "1") {
      clearTimeout(confirmTimer);
      submitExam(false);
      return;
    }
    // In-page confirmation: a browser dialog could knock the page out of fullscreen.
    el.submit.dataset.confirm = "1";
    el.submit.textContent = "Click again to confirm";
    showBanner("Ending the exam. Only solutions sent with Submit solution are scored.");
    confirmTimer = setTimeout(function () {
      el.submit.dataset.confirm = "";
      el.submit.textContent = "Submit exam";
    }, 5000);
  });

  // ---------------------------------------------------------------- gate button
  el.gateButton.addEventListener("click", function () {
    el.gateButton.disabled = true;
    el.gateError.hidden = true;
    requestFullscreen().then(function () {
      if (state.status === "pending") {
        return request(cfg.urls.begin, { method: "POST", fields: { fullscreen: "1" } }).then(function (r) {
          if (!r.ok) { throw fail(r); }
          applyState(r.data.state);
        });
      }
    }).then(function () {
      startRunning();
      return loadQuestions();
    }).then(function () {
      showExam();
    }).catch(function (error) {
      exitFullscreen();
      gateError(error.message || "Could not start. Please try again.");
    }).then(function () {
      el.gateButton.disabled = false;
    });
  });

  // ---------------------------------------------------------------- start
  updateWarnings();
  if (!supportsFullscreen()) {
    showGate("unsupported");
  } else if (state.status === "pending") {
    showGate("begin");
  } else {
    // Reloaded during an exam: the clock kept running on the server.
    showGate("reenter");
    startRunning();
  }
})();
