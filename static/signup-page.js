// Clinic self-service sign-up page.
  fetch("/api/signup-config").then(r => r.json()).then(c => {
    if (!c.enabled) { document.getElementById("form").innerHTML = "<p>Sign-up is closed. Contact us for an invitation.</p>"; }
    if (c.code_required) document.getElementById("codeField").hidden = false;
  });
  const form = document.getElementById("form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button"), body = Object.fromEntries(new FormData(form));
    btn.disabled = true; btn.textContent = "Creating…";
    const r = await fetch("/api/signup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    btn.disabled = false; btn.textContent = "Create clinic";
    if (!r.ok) { const d = await r.json().catch(() => ({})); document.getElementById("err").textContent = typeof d.detail === "string" ? d.detail : "Please check the form: clinic name, email and a password of 10+ characters."; return; }
    location.href = "/setup?welcome=1";
  };
