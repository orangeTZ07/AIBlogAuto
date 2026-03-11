// 基础工具：平滑滚动到目标元素
function smoothScrollTo(targetSelector) {
  var el = document.querySelector(targetSelector);
  if (!el) return;
  var rect = el.getBoundingClientRect();
  var offset = window.pageYOffset || document.documentElement.scrollTop || 0;
  // 预留顶部 header 高度
  var headerOffset = 76;
  var top = rect.top + offset - headerOffset;
  window.scrollTo({ top: top, behavior: "smooth" });
}

// 自动生成目录（从正文 h2 / h3 抽取）
function buildDynamicToc() {
  var article = document.querySelector(".article-body");
  var tocBody = document.querySelector(".toc-body");
  if (!article || !tocBody) return;

  var headings = article.querySelectorAll("h2.section-title, h3.subsection-title");
  if (!headings.length) return;

  // 清空原有列表
  tocBody.innerHTML = "";

  var topOl = document.createElement("ol");
  topOl.className = "toc-level-1";
  tocBody.appendChild(topOl);

  var currentH2Li = null;
  var currentH2SubOl = null;

  headings.forEach(function (node) {
    var level = node.tagName.toLowerCase() === "h2" ? 2 : 3;
    var section = node.closest(".article-section, .article-subsection");
    if (!section || !section.id) return;

    var text = node.textContent.trim();
    var li = document.createElement("li");
    li.className = "toc-item";
    var a = document.createElement("a");
    a.href = "#" + section.id;
    a.textContent = text;
    li.appendChild(a);

    if (level === 2) {
      topOl.appendChild(li);
      currentH2Li = li;
      currentH2SubOl = null;
    } else {
      if (!currentH2Li) {
        // 若没有 h2，直接挂在顶级
        topOl.appendChild(li);
        return;
      }
      if (!currentH2SubOl) {
        currentH2SubOl = document.createElement("ol");
        currentH2SubOl.className = "toc-level-2";
        currentH2Li.appendChild(currentH2SubOl);
      }
      currentH2SubOl.appendChild(li);
    }
  });
}

// 绑定 TOC / 顶部导航的点击事件
function bindNavigationClicks() {
  // 顶部快捷导航
  document.querySelectorAll(".quick-nav-item").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = btn.getAttribute("data-target");
      if (!target) return;
      smoothScrollTo(target);
    });
  });

  // 目录内部链接
  var toc = document.querySelector(".toc-body");
  if (toc) {
    toc.addEventListener("click", function (evt) {
      var a = evt.target.closest("a[href^='#']");
      if (!a) return;
      evt.preventDefault();
      var id = a.getAttribute("href");
      if (!id) return;
      smoothScrollTo(id);
    });
  }

  // 一键回主页：如果定义了 data-home-url 就优先使用
  var homeGate = document.querySelector(".home-gate");
  if (homeGate) {
    homeGate.addEventListener("click", function (evt) {
      var url = homeGate.getAttribute("data-home-url");
      if (url) {
        evt.preventDefault();
        window.location.href = url;
      }
      // 否则走 href 默认行为
    });
  }
}

// 滚动进度条 + TOC 高亮 + Section 进入视口效果
function setupScrollSystems() {
  var progressBar = document.createElement("div");
  progressBar.className = "scroll-progress-bar";
  progressBar.innerHTML = '<div class="scroll-progress-inner"></div>';
  document.body.appendChild(progressBar);
  var progressInner = progressBar.querySelector(".scroll-progress-inner");

  var sections = Array.prototype.slice.call(
    document.querySelectorAll(".article-section")
  );

  function updateScrollState() {
    var scrollTop =
      window.pageYOffset || document.documentElement.scrollTop || 0;
    var docHeight =
      document.documentElement.scrollHeight -
      document.documentElement.clientHeight;
    var ratio = docHeight > 0 ? scrollTop / docHeight : 0;
    var width = Math.max(0, Math.min(100, ratio * 100));
    if (progressInner) {
      progressInner.style.width = width + "%";
    }

    // 选出当前 section
    var currentId = null;
    var viewportCenter = scrollTop + window.innerHeight * 0.3;
    sections.forEach(function (sec) {
      var rect = sec.getBoundingClientRect();
      var secTop = rect.top + scrollTop;
      if (secTop <= viewportCenter) {
        currentId = sec.id || currentId;
      }
      // 进入视口的淡入装饰
      var secBottom = secTop + rect.height;
      if (
        secTop < scrollTop + window.innerHeight * 0.9 &&
        secBottom > scrollTop + window.innerHeight * 0.1
      ) {
        sec.classList.add("is-in-view");
      }
    });

    // TOC 高亮
    if (currentId) {
      document
        .querySelectorAll(".toc-item a.toc-active")
        .forEach(function (a) {
          a.classList.remove("toc-active");
        });
      var active = document.querySelector(
        '.toc-item a[href="#' + currentId + '"]'
      );
      if (active) {
        active.classList.add("toc-active");
      }
    }
  }

  window.addEventListener("scroll", updateScrollState, { passive: true });
  window.addEventListener("resize", updateScrollState);
  updateScrollState();
}

// TOC 折叠开关
function setupTocToggle() {
  var toggleBtn = document.querySelector(".toc-toggle");
  var tocBody = document.querySelector(".toc-body");
  if (!toggleBtn || !tocBody) return;

  toggleBtn.addEventListener("click", function () {
    var expanded = toggleBtn.getAttribute("aria-expanded") === "true";
    var nowExpanded = !expanded;
    toggleBtn.setAttribute("aria-expanded", String(nowExpanded));
    if (nowExpanded) {
      tocBody.style.maxHeight = "60vh";
      tocBody.style.overflow = "auto";
    } else {
      tocBody.style.maxHeight = "0";
      tocBody.style.overflow = "hidden";
    }
  });
}

// 初始化入口
document.addEventListener("DOMContentLoaded", function () {
  buildDynamicToc();
  bindNavigationClicks();
  setupScrollSystems();
  setupTocToggle();
});
