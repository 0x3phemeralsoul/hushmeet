let joined = false;

function hasJoinedCallControl() {
  const buttons = [...document.querySelectorAll("button[aria-label]")];
  return buttons.some((button) => /leave call|hang up|end call/i.test(button.getAttribute("aria-label") || ""));
}

function checkCallState() {
  const inCall = hasJoinedCallControl();
  if (inCall && !joined) {
    chrome.runtime.sendMessage({ type: "joined-meet" });
  } else if (!inCall && joined) {
    chrome.runtime.sendMessage({ type: "left-meet" });
  }
  joined = inCall;
}

setInterval(checkCallState, 1500);
checkCallState();
window.addEventListener("pagehide", () => {
  if (joined) chrome.runtime.sendMessage({ type: "left-meet" });
});
