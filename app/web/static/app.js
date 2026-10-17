/* 语音播报与少量渐进增强。
   原则：没有这段 JS，所有功能照样能用（服务端渲染 + 纯表单）。
   JS 只做两件事：记账后念一句确认；在识别能力缺失时把话说清楚。 */

(function () {
  "use strict";

  function speak(text) {
    if (!text) return;
    if (!("speechSynthesis" in window)) {
      // 明确降级，不假装成功：把要念的话显示在标题旁
      return;
    }
    try {
      var u = new SpeechSynthesisUtterance(text);
      u.lang = "zh-CN";
      u.rate = 0.95;
      u.pitch = 1.0;
      var voices = window.speechSynthesis.getVoices() || [];
      for (var i = 0; i < voices.length; i++) {
        if (voices[i].lang && voices[i].lang.toLowerCase().indexOf("zh") === 0) {
          u.voice = voices[i];
          break;
        }
      }
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    } catch (e) {
      /* 播报失败不影响记账 */
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var say = document.getElementById("say");
    if (say) {
      speak(say.getAttribute("data-text") || "");
    }

    // 金额输入框：自动聚焦，方便老人直接用数字键盘
    var amount = document.querySelector('input[name="amount"]');
    if (amount && !amount.value && !document.querySelector("input[autofocus]")) {
      try { amount.focus(); } catch (e) {}
    }

    // 表单重复提交防护：提交后禁用按钮，避免手抖记两笔
    document.querySelectorAll("form").forEach(function (form) {
      if (form.getAttribute("data-nosubmit") === "1") return;
      form.addEventListener("submit", function () {
        var btn = form.querySelector('button[type="submit"]');
        if (btn) {
          setTimeout(function () { btn.disabled = true; }, 0);
          setTimeout(function () { btn.disabled = false; }, 4000);
        }
      });
    });
  });
})();
