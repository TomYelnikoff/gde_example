// Robot mascot: eyes follow the pointer, click makes it hop.
(() => {
  let frame = null;

  function look(x, y) {
    document.querySelectorAll(".hero-robot .robot").forEach((svg) => {
      const r = svg.getBoundingClientRect();
      if (!r.width) return;
      const dx = Math.max(-1, Math.min(1, (x - (r.left + r.width / 2)) / (window.innerWidth / 2)));
      const dy = Math.max(-1, Math.min(1, (y - (r.top + r.height * 0.4)) / (window.innerHeight / 2)));
      svg.style.setProperty("--ex", `${(dx * 7).toFixed(1)}px`);
      svg.style.setProperty("--ey", `${(dy * 4).toFixed(1)}px`);
    });
    frame = null;
  }

  window.addEventListener("pointermove", (e) => {
    if (frame === null) frame = requestAnimationFrame(() => look(e.clientX, e.clientY));
  }, { passive: true });

  document.addEventListener("click", (e) => {
    const hero = e.target.closest(".hero-robot");
    if (!hero) return;
    hero.classList.remove("hop");
    void hero.offsetWidth; // restart the animation
    hero.classList.add("hop");
    setTimeout(() => hero.classList.remove("hop"), 800);
  });
})();
