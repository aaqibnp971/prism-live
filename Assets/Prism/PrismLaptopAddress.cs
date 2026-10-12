using System;
using System.Globalization;

namespace Prism
{
    /// Which laptop the headset connects to, decided once at app start with no network
    /// involved: nothing is discovered, broadcast or listened for.
    ///
    ///   1. laptop-ip.txt in the app's own storage, pushed over USB:
    ///        adb shell "echo 192.168.137.1 > /sdcard/Android/data/<package>/files/laptop-ip.txt"
    ///      (delete the file to go back to the build's address; the file is read when the
    ///      app starts, so restart the app after changing it)
    ///   2. otherwise the scene's Laptop Ip
    ///   3. otherwise 192.168.137.1, the laptop's Windows Mobile Hotspot address
    ///
    /// A file or scene value is used only if it is one plain private IPv4 address, so a bad
    /// or planted file can never point the headset at a name, another port, a public host or
    /// its own loopback. Kept free of Unity types so every case is tested
    /// (PrismLaptopAddressTests).
    public static class PrismLaptopAddress
    {
        public const string FileName = "laptop-ip.txt";
        public const string Default = "192.168.137.1";
        public const int MaxFileChars = 64;            // an address and a line ending, no more

        public struct Choice
        {
            public string address;     // always a usable address
            public string source;      // for the log: where it came from, and why not the file
        }

        /// fileText is the file's whole content, or null when there is no file. fileProblem
        /// is why the file could not be read at all (null when it could, or was absent).
        /// allowLoopback is true only in the Unity Editor, where Play mode uses 127.0.0.1.
        public static Choice Resolve(string fileText, string fileProblem, string sceneValue, bool allowLoopback)
        {
            string why;
            if (fileProblem != null) why = FileName + " unreadable (" + Printable(fileProblem) + ")";
            else if (fileText == null) why = "no " + FileName;
            else if (TryParse(fileText, false, out string fromFile, out string reason))
                return new Choice { address = fromFile, source = FileName };
            else why = FileName + " ignored: " + reason;

            if (TryParse(sceneValue, allowLoopback, out string fromScene, out string sceneReason))
                return new Choice { address = fromScene, source = "the build's Laptop Ip (" + why + ")" };
            return new Choice { address = Default, source = "the default (" + why + "; the build's Laptop Ip: " + sceneReason + ")" };
        }

        /// One dotted IPv4 address, optionally surrounded by spaces, a line ending or a
        /// byte-order mark, and nothing else. Private ranges only (10/8, 172.16/12,
        /// 192.168/16); 127.x only when allowLoopback.
        public static bool TryParse(string text, bool allowLoopback, out string address, out string reason)
        {
            address = null;
            if (text == null) { reason = "empty"; return false; }
            if (text.Length > MaxFileChars) { reason = "longer than " + MaxFileChars + " characters"; return false; }
            string s = text.Trim(' ', '\t', '\r', '\n', '\uFEFF');
            if (s.Length == 0) { reason = "empty"; return false; }

            string[] parts = s.Split('.');
            var octets = new int[4];
            bool shaped = parts.Length == 4;
            for (int i = 0; shaped && i < 4; i++)
            {
                string p = parts[i];
                shaped = p.Length >= 1 && p.Length <= 3 && (p.Length == 1 || p[0] != '0');
                foreach (char c in p) shaped &= c >= '0' && c <= '9';
                if (shaped)
                {
                    octets[i] = int.Parse(p, NumberStyles.None, CultureInfo.InvariantCulture);
                    shaped = octets[i] <= 255;
                }
            }
            if (!shaped) { reason = "'" + Printable(s) + "' is not a plain IPv4 address"; return false; }

            bool loopback = octets[0] == 127;
            bool isPrivate = octets[0] == 10 ||
                             (octets[0] == 172 && octets[1] >= 16 && octets[1] <= 31) ||
                             (octets[0] == 192 && octets[1] == 168);
            if (!(isPrivate || (loopback && allowLoopback)))
            {
                reason = "'" + s + "' is not an address on a private network";
                return false;
            }
            // .0 and .255 in the last place are a network's own and broadcast addresses on the
            // usual home, router and hotspot /24: never a laptop there. (The link is TCP, so
            // even a broadcast address on another size of network could only fail to connect.)
            if (!loopback && (octets[3] == 0 || octets[3] == 255))
            {
                reason = "'" + s + "' is a network or broadcast address";
                return false;
            }
            address = octets[0] + "." + octets[1] + "." + octets[2] + "." + octets[3];
            reason = null;
            return true;
        }

        /// Safe to put in a log line: short, and only ordinary visible characters.
        static string Printable(string s)
        {
            if (s == null) return "";
            var clean = new System.Text.StringBuilder();
            foreach (char c in s)
            {
                if (clean.Length >= 40) { clean.Append("..."); break; }
                clean.Append(c >= ' ' && c <= '~' && c != '\'' ? c : '?');
            }
            return clean.ToString();
        }
    }
}
