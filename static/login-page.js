// Staff login page.
  fetch("/api/signup-config").then(r => r.json()).then(c => { if (!c.enabled) document.getElementById("signupLine").hidden = true; });
  const form = document.getElementById("form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(form), btn = form.querySelector("button");
    btn.disabled = true;
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: f.get("email"), password: f.get("password") }) });
    btn.disabled = false;
    if (!r.ok) { const d = await r.json().catch(() => ({})); document.getElementById("err").textContent = typeof d.detail === "string" ? d.detail : "Login failed"; return; }
    const next = new URLSearchParams(location.search).get("next");
    location.href = next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
  };
