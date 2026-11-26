using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;

class Program
{
    static int Main(string[] args)
    {
        bool interactive = args.Length == 0;
        try
        {
            string option = "";
            if (args.Length == 1 && args[0] == "--no-browser") option = " -NoBrowser";
            else if (args.Length == 1 && args[0] == "--stop") option = " -Stop";
            else if (!interactive) throw new ArgumentException("Options: --no-browser, --stop");
            Console.Title = "PSV Prototype | Odoo + IoT";
            using (var gate = new Mutex(false, @"Local\PSVPrototypeSetup"))
            {
                try { gate.WaitOne(); } catch (AbandonedMutexException) { }
                string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PSV-Prototype");
                Directory.CreateDirectory(root);
                string payload;
                using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                using (var hash = SHA256.Create())
                {
                    payload = Path.Combine(root, BitConverter.ToString(hash.ComputeHash(input)).Replace("-", ""));
                    if (!Directory.Exists(payload))
                    {
                        string stage = payload + "." + Guid.NewGuid().ToString("N");
                        Directory.CreateDirectory(stage);
                        input.Position = 0;
                        using (var archive = new ZipArchive(input, ZipArchiveMode.Read))
                            archive.ExtractToDirectory(stage);
                        Directory.Move(stage, payload);
                    }
                }
                string shell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
                var start = new ProcessStartInfo(shell, "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(payload, "start.ps1") + "\"" + option);
                start.UseShellExecute = false;
                start.WorkingDirectory = payload;
                using (var process = Process.Start(start))
                {
                    process.WaitForExit();
                    if (process.ExitCode != 0) throw new Exception("Startup failed (" + process.ExitCode + ").");
                }
            }
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.Message);
            if (interactive) { Console.WriteLine("Press Enter to close."); Console.ReadLine(); }
            return 1;
        }
    }
}
