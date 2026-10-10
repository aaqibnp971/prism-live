using System.IO;
using System.Text.RegularExpressions;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace Prism.Tests
{
    /// The headset must never show an unlinked world as if it were a real session.
    /// Run from Window > General > Test Runner > EditMode, or in batch mode.
    public class PrismLinkStatusTests
    {
        [Test]
        public void TheRedDotShowsInEveryStateExceptARealLiveLink()
        {
            int hidden = 0;
            foreach (bool preview in new[] { false, true })
                foreach (bool open in new[] { false, true })
                    foreach (bool haveState in new[] { false, true })
                        foreach (bool stale in new[] { false, true })
                        {
                            bool shown = PrismLinkStatus.ShowMarker(preview, open, haveState, stale);
                            Assert.AreEqual(!shown, PrismLinkStatus.Linked(preview, open, haveState, stale));
                            if (!shown) hidden++;
                        }
            // Of the sixteen states exactly one hides the dot: a real socket, open, that has
            // delivered a state and has not gone quiet.
            Assert.AreEqual(1, hidden);
            Assert.IsFalse(PrismLinkStatus.ShowMarker(false, true, true, false));
        }

        [Test]
        public void AnOpenSocketThatHasSentNoStateIsNotALink()
        {
            Assert.IsTrue(PrismLinkStatus.ShowMarker(false, true, false, false));
        }

        [Test]
        public void TheBuildLabelSaysWhenABuildCarriesNoStamp()
        {
            Assert.AreEqual("0.1.0+20261010-1432-abc1234",
                PrismLinkStatus.BuildLabel("0.1.0", "20261010-1432-abc1234\n"));
            Assert.AreEqual("0.1.0+unstamped", PrismLinkStatus.BuildLabel("0.1.0", null));
            Assert.AreEqual("0.1.0+unstamped", PrismLinkStatus.BuildLabel("0.1.0", " "));
        }

        [Test]
        public void OfflinePreviewIsNeverLinkedAndAlwaysLabelled()
        {
            // PrismLink sets open = true in Offline Preview so the pretend session can run;
            // that must not hide the red dot or the label.
            Assert.IsTrue(PrismLinkStatus.ShowMarker(true, true, true, false));
            Assert.IsTrue(PrismLinkStatus.ShowOfflineLabel(true));
            Assert.IsFalse(PrismLinkStatus.ShowOfflineLabel(false));
            StringAssert.Contains("OFFLINE PREVIEW", PrismLinkStatus.OfflineLabel);
        }

        [Test]
        public void TheSceneThatGetsBuiltIsTheBoothScene()
        {
            var problems = PrismBuild.SceneProblems(out string scenePath);
            Assert.IsEmpty(problems, string.Join("\n", problems));
            StringAssert.EndsWith("SampleScene.unity", scenePath);
        }

        [Test]
        public void TheRedDotShaderIsReferencedByTheSceneSoABuildIncludesIt()
        {
            // Shader.Find fails in a build for a shader nothing references; the marker would
            // then fall back to a material the build may not carry.
            string guid = AssetDatabase.AssetPathToGUID("Assets/Prism/PrismMarker.shader");
            Assert.IsNotEmpty(guid);
            string scene = File.ReadAllText("Assets/Scenes/SampleScene.unity");
            StringAssert.IsMatch(@"markerShader: \{fileID: 4800000, guid: " + Regex.Escape(guid), scene);
        }

        [Test]
        public void AndroidAlwaysRequestsTheInternetPermission()
        {
            // "Auto" leaves the permission out when Unity sees no UnityWebRequest; PrismLink
            // uses a raw WebSocket, which Unity does not detect.
            Assert.IsTrue(PlayerSettings.Android.forceInternetPermission);
        }
    }
}
