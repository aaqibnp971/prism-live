using System.IO;
using NUnit.Framework;
using UnityEngine;

namespace Prism.Tests
{
    /// The laptop's address without a rebuild: a file wins only when it is one plain private
    /// IPv4 address; anything else falls back, and the headset always has an address.
    public class PrismLaptopAddressTests
    {
        const string Scene = "192.168.137.1";

        static PrismLaptopAddress.Choice Device(string file, string scene = Scene, string problem = null) =>
            PrismLaptopAddress.Resolve(file, problem, scene, false);

        [Test]
        public void AValidFileWinsHoweverAdbWroteIt()
        {
            foreach (string written in new[] { "192.168.1.175", "192.168.1.175\n", "192.168.1.175\r\n",
                                               "  192.168.1.175 \n", "\uFEFF192.168.1.175\r\n", "\t192.168.1.175" })
            {
                var choice = Device(written);
                Assert.AreEqual("192.168.1.175", choice.address, written);
                Assert.AreEqual("laptop-ip.txt", choice.source);
            }
            Assert.AreEqual("10.0.0.7", Device("10.0.0.7").address);
            Assert.AreEqual("172.16.4.2", Device("172.16.4.2").address);
            Assert.AreEqual("172.31.255.254", Device("172.31.255.254").address);
            Assert.AreEqual("172.16.0.1", Device("172.16.0.1").address);
            // exactly at the length limit: a valid address padded to 64 characters
            Assert.AreEqual("192.168.1.175", Device("192.168.1.175" + new string(' ', 51)).address);
            Assert.AreEqual(Scene, Device("192.168.1.175" + new string(' ', 52)).address);
        }

        [Test]
        public void NoFileMeansTheBuildsAddressAndTheLogSaysSo()
        {
            var choice = Device(null);
            Assert.AreEqual(Scene, choice.address);
            StringAssert.Contains("the build's Laptop Ip", choice.source);
            StringAssert.Contains("no laptop-ip.txt", choice.source);
        }

        [TestCase("")]
        [TestCase("\n")]
        [TestCase("   ")]
        [TestCase("laptop")]
        [TestCase("laptop.local")]
        [TestCase("192.168.1")]
        [TestCase("192.168.1.175.3")]
        [TestCase("192.168.1.256")]
        [TestCase("192.168.1.-1")]
        [TestCase("192.168.01.175")]            // a leading zero: octal to some parsers
        [TestCase("192.168.1.175:8787")]
        [TestCase("ws://192.168.1.175")]
        [TestCase("192.168.1.175/live")]
        [TestCase("192.168.1.175 192.168.1.176")]
        [TestCase("192.168.1.175\n192.168.1.176")]
        [TestCase("192.168.1.175\n# the booth laptop")]
        [TestCase("192.168. 1.175")]
        [TestCase("192.168.137.1.")]            // a trailing dot
        [TestCase("+192.168.1.175")]
        [TestCase("192.168.1.1e2")]
        [TestCase("192.168.1.175\0")]           // an embedded NUL
        [TestCase("\u00A0192.168.1.175")]       // padding that is not a plain space
        [TestCase("\uFF11\uFF10.0.0.7")]        // full-width digits
        [TestCase("10.0.0.0")]
        [TestCase("1\09\02\0.\01\06\08\0.\01\0.\05\0")]   // UTF-16 read without its mark
        [TestCase("0x7f.0.0.1")]
        [TestCase("3232235951")]                // an address as one number
        [TestCase("::1")]
        [TestCase("fe80::1")]
        [TestCase("١٩٢.١٦٨.١.١٧٥")]            // digits that are not 0-9
        [TestCase("8.8.8.8")]                   // public
        [TestCase("172.32.0.1")]                // just outside 172.16/12
        [TestCase("172.15.0.1")]
        [TestCase("192.169.1.1")]
        [TestCase("169.254.10.10")]             // link-local
        [TestCase("127.0.0.1")]                 // the headset itself
        [TestCase("0.0.0.0")]
        [TestCase("255.255.255.255")]           // broadcast
        [TestCase("192.168.137.255")]           // the hotspot's broadcast address
        [TestCase("192.168.137.0")]
        [TestCase("224.0.0.1")]                 // multicast
        public void AnythingElseInTheFileFallsBackToTheBuildsAddress(string file)
        {
            var choice = Device(file);
            Assert.AreEqual(Scene, choice.address, file);
            StringAssert.Contains("laptop-ip.txt ignored", choice.source);
            StringAssert.DoesNotContain("\n", choice.source);
        }

        [Test]
        public void AnOverlongOrUnreadableFileFallsBackAndNeverThrows()
        {
            Assert.AreEqual(Scene, Device(new string('1', 65)).address);
            Assert.AreEqual(Scene, Device("192.168.1.175" + new string(' ', 60)).address);
            var unreadable = Device(null, Scene, "UnauthorizedAccessException");
            Assert.AreEqual(Scene, unreadable.address);
            StringAssert.Contains("unreadable", unreadable.source);
            // text is never trusted because a read "also" reported a problem
            Assert.AreEqual(Scene, Device("192.168.1.175", Scene, "IOException").address);
            StringAssert.DoesNotContain("\n", Device("192.168.1.\u0000\n\u001b[31m").source);
        }

        [TestCase(null)]
        [TestCase("")]
        [TestCase("127.0.0.1")]
        [TestCase("localhost")]
        [TestCase("8.8.8.8")]
        [TestCase("192.168.137.255")]
        public void ABadBuildAddressEndsAtTheHotspotDefault(string scene)
        {
            var choice = Device(null, scene);
            Assert.AreEqual("192.168.137.1", choice.address);
            Assert.AreEqual(PrismLaptopAddress.Default, choice.address);
            StringAssert.Contains("the default", choice.source);
            StringAssert.Contains("the build's Laptop Ip: ", choice.source);
            // an unreadable file with a bad build address ends there too
            Assert.AreEqual("192.168.137.1", Device(null, scene, "IOException").address);
            // and a bad file with a bad build address still ends there
            Assert.AreEqual("192.168.137.1", Device("not an address", scene).address);
            // while a good file still wins over a bad build address
            Assert.AreEqual("10.1.2.3", Device("10.1.2.3", scene).address);
        }

        [Test]
        public void LoopbackIsForEditorPlayModeOnlyAndNeverFromTheFile()
        {
            Assert.AreEqual("127.0.0.1", PrismLaptopAddress.Resolve(null, null, "127.0.0.1", true).address);
            Assert.AreEqual(Scene, PrismLaptopAddress.Resolve("127.0.0.1", null, Scene, true).address);
            Assert.AreEqual("192.168.137.1", PrismLaptopAddress.Resolve(null, null, "127.0.0.1", false).address);
        }

        [Test]
        public void EveryOutcomeIsAnAddressTheParserItselfAccepts()
        {
            string[] inputs = { null, "", "x", "192.168.1.175", "8.8.8.8", "127.0.0.1", "10.0.0.1\n", "1.2.3", "999.1.1.1" };
            foreach (string file in inputs)
                foreach (string scene in inputs)
                    foreach (bool editor in new[] { false, true })
                    {
                        string address = PrismLaptopAddress.Resolve(file, null, scene, editor).address;
                        Assert.IsTrue(PrismLaptopAddress.TryParse(address, editor, out string again, out _), address);
                        Assert.AreEqual(address, again);
                    }
        }

        [Test]
        public void TheSceneAndTheLinkUseTheHotspotAddressAndTheResolvedOne()
        {
            string scene = File.ReadAllText("Assets/Scenes/SampleScene.unity");
            StringAssert.Contains("  laptopIp: 192.168.137.1\n", scene.Replace("\r\n", "\n"));
            string link = File.ReadAllText(Path.Combine(Application.dataPath, "Prism", "PrismLink.cs"));
            StringAssert.Contains("public string laptopIp = \"192.168.137.1\";", link);
            // the socket is opened to the resolved address, never to the raw field
            StringAssert.Contains("string Url => $\"ws://{address}:{port}/live\";", link);
            StringAssert.Contains("PrismLaptopAddress.Resolve(", link);
            // nothing here discovers or announces anything
            foreach (string net in new[] { "UdpClient", "Broadcast", "Multicast", "Dns.", "TcpListener", "HttpListener" })
                StringAssert.DoesNotContain(net, link);
        }
    }
}
