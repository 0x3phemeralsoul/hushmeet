chrome.runtime.onMessage.addListener((message, sender) => {
  if (!["joined-meet", "left-meet"].includes(message.type) || !sender.tab?.url) return;
  chrome.runtime.sendNativeMessage(
    "io.hushmeet.recorder",
    {
      meet_url: sender.tab.url,
      action: message.type === "joined-meet" ? "joined" : "left",
    },
    (response) => {
      const nativeError = chrome.runtime.lastError;
      if (nativeError) {
        console.error("HushMeet native messaging failed:", nativeError.message);
      } else if (!response?.ok) {
        console.error("HushMeet rejected the Meet event:", response.message || response.error || "unknown error");
      }
    }
  );
});
