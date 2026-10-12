using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Prism
{
    /// The one way to build the headset app that cannot end quietly: Prism > Build headset APK,
    /// or from a terminal with the Editor closed:
    ///   Unity.exe -batchmode -quit -projectPath C:\ANP\PrismVR -buildTarget Android
    ///             -executeMethod Prism.PrismBuild.Quest -logFile -
    /// It checks the scene first, deletes the old APK so a failed build leaves none behind, and
    /// ends with PRISM BUILD OK or PRISM BUILD FAILED (exit code 1 in batch mode).
    /// On 10 October 2026 three failed Build And Run attempts left a four-day-old app on the
    /// headset, still in Offline Preview, and it looked like a real session.
    public static class PrismBuild
    {
        public const string ApkPath = "Builds/prism.apk";
        public const string StampPath = "Assets/Resources/PrismBuildStamp.txt";
        const string MarkerShaderPath = "Assets/Prism/PrismMarker.shader";
        /// Set while Quest() builds: the scene check then refuses instead of warning.
        internal static bool Strict;
        const string LinkClass = "m_EditorClassIdentifier: Assembly-CSharp::Prism.PrismLink";

        /// What is wrong with the scene a build would take, read from the saved file (an
        /// unsaved change in the Editor is not built either). Empty means it is the booth scene.
        public static List<string> SceneProblems(out string scenePath)
        {
            var problems = new List<string>();
            scenePath = null;
            var enabled = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
            if (enabled.Length != 1)
            {
                problems.Add($"The Scene List must enable exactly one scene; it enables {enabled.Length}.");
                return problems;
            }
            scenePath = enabled[0];
            if (!File.Exists(scenePath))
            {
                problems.Add($"The scene in the Scene List does not exist: {scenePath}");
                return problems;
            }
            string link = LinkBlock(File.ReadAllText(scenePath));
            if (link == null)
            {
                problems.Add($"{scenePath} has no PrismLink: it is not the Prism scene.");
                return problems;
            }
            string preview = Field(link, "offlinePreview");
            if (preview == null)
                problems.Add($"{scenePath}: the Prism Link's Offline Preview setting could not be read.");
            else if (preview != "0")
                problems.Add($"Offline Preview is ON in {scenePath}. Untick it on the Prism object's Prism Link.");
            // Without this reference a build leaves the shader out and the red dot may not draw.
            string markerGuid = AssetDatabase.AssetPathToGUID(MarkerShaderPath);
            if (string.IsNullOrEmpty(markerGuid) || !(Field(link, "markerShader") ?? "").Contains(markerGuid))
                problems.Add($"{scenePath}: Prism Link's Marker Shader is not set to Prism/Marker. " +
                             "Select the Prism object once in the Editor and save the scene.");
            return problems;
        }

        /// SceneProblems plus what only matters on the headset itself.
        public static List<string> DeviceProblems(out string scenePath)
        {
            var problems = SceneProblems(out scenePath);
            if (problems.Count > 0) return problems;
            // The same rule the headset applies at start (PrismLaptopAddress): a value it would
            // discard for the default must not pass the build quietly.
            string ip = Field(LinkBlock(File.ReadAllText(scenePath)), "laptopIp") ?? "";
            if (!PrismLaptopAddress.TryParse(ip, false, out _, out string reason))
                problems.Add($"Laptop Ip in {scenePath} cannot be used on the headset: {reason}. Set it to the " +
                             "laptop's address (192.168.137.1 on its own hotspot); laptop-ip.txt can still replace it.");
            if (!PlayerSettings.Android.forceInternetPermission)
                problems.Add("Player Settings > Android > Internet Access must be Require.");
            return problems;
        }

        static string LinkBlock(string sceneYaml)
        {
            int at = sceneYaml.IndexOf(LinkClass, StringComparison.Ordinal);
            if (at < 0) return null;
            int end = sceneYaml.IndexOf("\n--- ", at, StringComparison.Ordinal);
            return end < 0 ? sceneYaml.Substring(at) : sceneYaml.Substring(at, end - at);
        }

        static string Field(string block, string name)
        {
            var match = Regex.Match(block, @"^\s*" + Regex.Escape(name) + @":[ \t]*(.*?)\s*$", RegexOptions.Multiline);
            return match.Success ? match.Groups[1].Value : null;
        }

        [MenuItem("Prism/Build headset APK")]
        public static void Quest()
        {
            try { Build(); }
            catch (Exception error)
            {
                Finish(false, "PRISM BUILD FAILED: " + error.Message +
                              "\nNo new APK should be trusted; the headset still has whatever was installed before.");
            }
            finally { Strict = false; }
        }

        /// yyyyMMdd-HHmm (local time) and the commit, so logcat and the laptop's session log
        /// show which build is on the headset.
        static string Stamp()
        {
            string commit = "nogit";
            try
            {
                string head = File.ReadAllText(".git/HEAD").Trim();
                if (head.StartsWith("ref: "))
                {
                    string reference = Path.Combine(".git", head.Substring(5));
                    head = File.Exists(reference) ? File.ReadAllText(reference).Trim() : "";
                }
                if (head.Length >= 7) commit = head.Substring(0, 7);
            }
            catch (IOException) { }
            return DateTime.Now.ToString("yyyyMMdd-HHmm") + "-" + commit;
        }

        static void Build()
        {
            var problems = DeviceProblems(out string scenePath);
            if (problems.Count > 0)
            {
                Finish(false, "PRISM BUILD FAILED before building:\n" + string.Join("\n", problems));
                return;
            }

            // A failed build must not leave an old app looking like the new one.
            if (File.Exists(ApkPath)) File.Delete(ApkPath);
            Directory.CreateDirectory(Path.GetDirectoryName(ApkPath));

            string stamp = Stamp();
            File.WriteAllText(StampPath, stamp + "\n");
            AssetDatabase.ImportAsset(StampPath, ImportAssetOptions.ForceUpdate);
            Strict = true;

            BuildReport report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { scenePath },
                locationPathName = ApkPath,
                target = BuildTarget.Android,
                options = BuildOptions.None,
            });
            BuildSummary summary = report.summary;
            if (summary.result != BuildResult.Succeeded || !File.Exists(ApkPath))
            {
                Finish(false, $"PRISM BUILD FAILED: {summary.result}, {summary.totalErrors} error(s). " +
                              "No APK was written; the headset still has whatever was installed before.");
                return;
            }
            Finish(true, $"PRISM BUILD OK: {Path.GetFullPath(ApkPath)} ({new FileInfo(ApkPath).Length} bytes), " +
                         $"scene {scenePath}, build {PlayerSettings.bundleVersion}+{stamp}. " +
                         "Install it on the headset; building alone does not.");
        }

        static void Finish(bool ok, string message)
        {
            if (ok) Debug.Log(message); else Debug.LogError(message);
            if (Application.isBatchMode) EditorApplication.Exit(ok ? 0 : 1);
            else EditorUtility.DisplayDialog(ok ? "Prism build finished" : "Prism build FAILED", message, "OK");
        }
    }

    /// Build And Run from the Build Profiles window still works; it only warns. The headset
    /// itself then shows the OFFLINE PREVIEW label and the red dot (PrismLink).
    class PrismSceneCheck : IProcessSceneWithReport
    {
        public int callbackOrder => 0;

        public void OnProcessScene(Scene scene, BuildReport report)
        {
            if (report == null) return;                       // entering Play mode, not a build
            foreach (GameObject root in scene.GetRootGameObjects())
                foreach (PrismLink link in root.GetComponentsInChildren<PrismLink>(true))
                {
                    if (!link.offlinePreview) continue;
                    // Checked on the real objects, so a second Prism Link or a prefab counts.
                    if (PrismBuild.Strict)
                        throw new BuildFailedException($"Offline Preview is ON in {scene.path}.");
                    Debug.LogWarning($"PRISM: Offline Preview is ON in {scene.path}. This build will not " +
                                     "follow the laptop; the headset will say OFFLINE PREVIEW.");
                }
        }
    }
}
