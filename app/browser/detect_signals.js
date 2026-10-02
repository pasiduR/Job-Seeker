// Raw signals for stop conditions in one document; Python decides what they mean.
() => {
  const CAPTCHA_SRC = /recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha|captcha/i;
  const CAPTCHA_SELECTOR =
    ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #challenge-form, iframe[title*='captcha' i]";
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const captchaFrames = Array.from(document.querySelectorAll("iframe")).filter((frame) =>
    CAPTCHA_SRC.test(frame.getAttribute("src") || "")
  );
  return {
    captcha: captchaFrames.length > 0 || document.querySelector(CAPTCHA_SELECTOR) !== null,
    password_fields: Array.from(document.querySelectorAll("input[type=password]")).filter(visible).length,
    text: (document.body ? document.body.innerText : "").slice(0, 5000),
  };
}
