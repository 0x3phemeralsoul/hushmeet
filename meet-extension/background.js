chrome.runtime.onMessage.addListener((message, sender) => {
  if (!["joined-meet", "left-meet"].includes(message.type) || !sender.tab?.url) return;
  chrome.runtime.sendNativeMessage(
    "io.hushmeet.recorder",
    {
      meet_url: sender.tab.url,
      action: message.type === "joined-meet" ? "joined" : "left",
    },
    () => void chrome.runtime.lastError
  );
});
