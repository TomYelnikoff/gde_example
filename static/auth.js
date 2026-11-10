(() => {
  const form = document.getElementById("auth-form");
  const errorEl = document.getElementById("auth-error");
  const submit = document.getElementById("auth-submit");
  const mode = form.dataset.mode; // "login" | "signup"
  const label = submit.textContent;

  const robot = document.querySelector(".hero-robot");

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
    if (msg && robot) { // the robot shakes its head
      robot.classList.remove("shake");
      void robot.offsetWidth;
      robot.classList.add("shake");
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");

    const email = form.email.value.trim();
    const password = form.password.value;

    if (!email || !password) return showError("Please enter your email and password.");
    if (mode === "signup") {
      if (password.length < 8) return showError("Password must be at least 8 characters.");
      if (password !== form.confirm.value) return showError("Passwords don't match.");
    }

    submit.disabled = true;
    submit.textContent = "Please wait…";
    try {
      const res = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `Request failed (${res.status})`);
      }
      window.location.replace("/");
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
      submit.textContent = label;
    }
  });
})();
