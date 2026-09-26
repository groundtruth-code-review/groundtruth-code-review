// Shared behaviour for the landing page and every guide page: a copy
// button on every code block, a permalink on every heading that doesn't
// also toggle the <details> it sits in, deep-linking into a collapsed
// Q&A entry, and a scrollspy on the "on this page" rail. No build step,
// no dependency -- this file is loaded as-is by both docs/index.html and
// every page docs/build_guide.py generates.
(function () {
  "use strict";

  function addCopyButtons() {
    document.querySelectorAll("pre").forEach(function (pre) {
      if (pre.querySelector(".copy-btn")) return;
      var code = pre.querySelector("code") || pre;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      btn.textContent = "Copy";
      btn.addEventListener("click", function () {
        var text = code.textContent;
        var announce = function (label) {
          var original = "Copy";
          btn.textContent = label;
          btn.disabled = true;
          setTimeout(function () {
            btn.textContent = original;
            btn.disabled = false;
          }, 1600);
        };
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
              function () { announce("Copied"); },
              function () {}
            );
          }
        } catch (err) {}
      });
      pre.appendChild(btn);
    });
  }

  // A heading's permalink lives inside <summary> on a Q&A page, and a
  // <summary> click is what native <details> uses to open and close --
  // without this the link would both navigate and toggle the entry shut.
  function stopAnchorsTogglingDetails() {
    document.querySelectorAll(".h-anchor").forEach(function (a) {
      a.addEventListener("click", function (e) { e.stopPropagation(); });
    });
  }

  function openDetailsForHash() {
    if (!location.hash) return;
    var target = document.getElementById(location.hash.slice(1));
    var details = target && target.closest ? target.closest("details") : null;
    if (!details) return;
    details.open = true;
    setTimeout(function () { details.scrollIntoView({ block: "start" }); }, 0);
  }

  function buildScrollspy() {
    var links = document.querySelectorAll(".toc a[href^='#']");
    if (!links.length || !("IntersectionObserver" in window)) return;
    var linkFor = {};
    links.forEach(function (a) { linkFor[a.getAttribute("href").slice(1)] = a; });

    var headings = Array.prototype.filter.call(
      document.querySelectorAll(".doc-body h2[id]"),
      function (h) { return linkFor[h.id]; }
    );
    if (!headings.length) return;

    var active = null;
    var setActive = function (link) {
      if (active === link) return;
      if (active) active.classList.remove("active");
      if (link) link.classList.add("active");
      active = link;
    };
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) setActive(linkFor[entry.target.id]);
        });
      },
      { rootMargin: "-15% 0px -70% 0px", threshold: 0 }
    );
    headings.forEach(function (h) { observer.observe(h); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    addCopyButtons();
    stopAnchorsTogglingDetails();
    openDetailsForHash();
    buildScrollspy();
  });
  window.addEventListener("hashchange", openDetailsForHash);
})();
