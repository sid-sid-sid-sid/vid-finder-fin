using Microsoft.Win32;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;

namespace filmfinder
{
    public partial class MainWindow : Window
    {
        private const string PythonExe = "python";
        private static readonly string ScriptPath =
            Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "main.exe");

        private string? _selectedFile;

        public MainWindow()
        {
            InitializeComponent();
        }

        private void Upload_Click(object sender, RoutedEventArgs e)
        {
            var dlg = new OpenFileDialog
            {
                Title = "Select a video file",
                Filter = "Video files|*.mp4;*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.m4v|All files|*.*",
            };

            if (dlg.ShowDialog() == true)
            {
                _selectedFile = dlg.FileName;
                FilePathText.Text = _selectedFile;
                FilePathText.Foreground = System.Windows.Media.Brushes.White;
                RunButton.IsEnabled = true;
                SetStatus("");
            }
        }

        private async void Run_Click(object sender, RoutedEventArgs e)
        {
            if (_selectedFile is null) return;

            SetBusy(true);
            SetStatus("Analysing video…  (this can take a minute)");

            try
            {
                var result = await RunPythonAsync(_selectedFile);
                HandleResult(result);
            }
            catch (Exception ex)
            {
                SetStatus($"  Unexpected error: {ex.Message}");
            }
            finally
            {
                SetBusy(false);
            }
        }



        private static Task<PythonResult> RunPythonAsync(string videoPath)
        {
            return Task.Run(() =>
            {
                var psi = new ProcessStartInfo
                {
                    FileName = ScriptPath,

                    Arguments = $"\"{videoPath}\" --json",

                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };

                using var proc = new Process { StartInfo = psi };
                proc.Start();

                string stdout = proc.StandardOutput.ReadToEnd();
                string stderr = proc.StandardError.ReadToEnd();
                proc.WaitForExit();

                string jsonLine = stdout.Trim()
                    .Split('\n')
                    .LastOrDefault(l => l.TrimStart().StartsWith("{"))
                    ?.Trim() ?? "";

                if (string.IsNullOrEmpty(jsonLine))
                {
                    return new PythonResult
                    {
                        Error = string.IsNullOrWhiteSpace(stderr)
                            ? "No output from script."
                            : stderr.Trim()
                    };
                }

                try
                {
                    using var doc = JsonDocument.Parse(jsonLine);
                    var root = doc.RootElement;

                    return new PythonResult
                    {
                        Title = root.TryGetProperty("title", out var t) ? t.GetString() : null,
                        YoutubeUrl = root.TryGetProperty("youtube_url", out var u) ? u.GetString() : null,
                        Error = root.TryGetProperty("error", out var er) ? er.GetString() : null,
                    };
                }
                catch
                {
                    return new PythonResult { Error = $"Bad JSON from script: {jsonLine}" };
                }
            });
        }

        private void HandleResult(PythonResult r)
        {
            if (r.Error is not null)
            {
                SetStatus($"  Error: {r.Error}");
                return;
            }

            bool hasTitle = !string.IsNullOrWhiteSpace(r.Title);
            bool hasUrl = !string.IsNullOrWhiteSpace(r.YoutubeUrl);

            if (!hasTitle && !hasUrl)
            {
                SetStatus("  Can't find video");
                return;
            }

            if (hasUrl)
            {
                SetStatus($"  Found: {r.Title ?? r.YoutubeUrl}  —  opening…");
                OpenUrl(r.YoutubeUrl!);
                return;
            }

            SetStatus($"⚠  No link found, video title: {r.Title}");
        }

        private static void OpenUrl(string url)
        {
            try
            {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                MessageBox.Show($"Couldn't open browser:\n{ex.Message}",
                    "FilmFinder", MessageBoxButton.OK, MessageBoxImage.Warning);
            }
        }

        private void SetBusy(bool busy)
        {
            RunButton.IsEnabled = !busy && _selectedFile is not null;
            UploadButton.IsEnabled = !busy;
            ProgressBar.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
            StatusText.Visibility = busy ? Visibility.Collapsed : Visibility.Visible;
        }

        private void SetStatus(string msg) => StatusText.Text = msg;

        private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

        private record PythonResult
        {
            public string? Title { get; init; }
            public string? YoutubeUrl { get; init; }
            public string? Error { get; init; }
        }
        private void Border_MouseDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton == MouseButtonState.Pressed)
                DragMove();
        }

    }
}
