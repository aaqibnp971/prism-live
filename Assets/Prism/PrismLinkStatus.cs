namespace Prism
{
    /// What the headset must show about its own link, kept apart from PrismLink so it can be
    /// tested without a scene (Assets/Prism/Editor/PrismLinkStatusTests.cs).
    /// The rule: a world that is not following the laptop never looks like a real session.
    public static class PrismLinkStatus
    {
        /// Always in view while Offline Preview is on, in the Editor and on the headset.
        public const string OfflineLabel = "OFFLINE PREVIEW\nNOT CONNECTED TO THE LAPTOP";

        /// Following the laptop: a real connection that has delivered a state and is still
        /// sending them. An open socket that has sent no state yet is not a link, and Offline
        /// Preview is never linked, however alive its pretend session looks.
        public static bool Linked(bool offlinePreview, bool open, bool haveState, bool stale) =>
            !offlinePreview && open && haveState && !stale;

        /// The red dot: on whenever the headset is not linked, in every mode.
        public static bool ShowMarker(bool offlinePreview, bool open, bool haveState, bool stale) =>
            !Linked(offlinePreview, open, haveState, stale);

        /// What the laptop is told in hello and what the log shows: the app version plus the
        /// stamp PrismBuild writes, so an old install cannot pass for a new one.
        public static string BuildLabel(string version, string stamp) =>
            string.IsNullOrWhiteSpace(stamp) ? version + "+unstamped" : version + "+" + stamp.Trim();

        public static bool ShowOfflineLabel(bool offlinePreview) => offlinePreview;
    }
}
