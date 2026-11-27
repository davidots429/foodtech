using System.Globalization;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

internal static class MockServerCertificate
{
    private const string CertificateVariable = "FOODTECH_MOCK_SERVER_CERT_PEM";
    private const string KeyVariable = "FOODTECH_MOCK_SERVER_KEY_PEM";

    public static int? RuntimeSeconds(Dictionary<string, string> options)
    {
        var source = options.GetValueOrDefault("certificate-source", "file");
        if (source is not ("file" or "injected"))
            throw new ArgumentException("--certificate-source must be file or injected.");
        if (!options.TryGetValue("max-runtime", out var text))
        {
            if (source == "injected") throw new ArgumentException("Injected REST Mock requires --max-runtime.");
            return null;
        }
        if (!int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out var seconds) || seconds is < 1 or > 600)
            throw new ArgumentException("REST Mock --max-runtime must be 1 to 600 seconds.");
        return seconds;
    }

    public static X509Certificate2 Load(Dictionary<string, string> options)
    {
        var source = options.GetValueOrDefault("certificate-source", "file");
        var pem = Environment.GetEnvironmentVariable(CertificateVariable);
        var key = Environment.GetEnvironmentVariable(KeyVariable);
        var password = Environment.GetEnvironmentVariable("FOODTECH_MOCK_CERT_PASSWORD");
        try
        {
            X509Certificate2 certificate;
            if (source == "injected")
            {
                if (options.ContainsKey("cert") || !string.IsNullOrEmpty(password))
                    throw new ArgumentException("Choose one REST Mock certificate source.");
                if (string.IsNullOrWhiteSpace(pem) || string.IsNullOrWhiteSpace(key) || pem.Length > 65536 || key.Length > 65536)
                    throw new ArgumentException("Injected REST Mock certificate and key are required within their size limits.");
                certificate = X509Certificate2.CreateFromPem(pem, key);
            }
            else if (source == "file")
            {
                if (!string.IsNullOrEmpty(pem) || !string.IsNullOrEmpty(key))
                    throw new ArgumentException("Choose one REST Mock certificate source.");
                if (!options.TryGetValue("cert", out var path) || string.IsNullOrWhiteSpace(path) || string.IsNullOrWhiteSpace(password))
                    throw new ArgumentException("REST Mock PFX path and password are required.");
                certificate = X509CertificateLoader.LoadPkcs12FromFile(path, password, X509KeyStorageFlags.EphemeralKeySet);
            }
            else throw new ArgumentException("--certificate-source must be file or injected.");
            if (!certificate.HasPrivateKey || certificate.NotBefore.ToUniversalTime() > DateTime.UtcNow ||
                certificate.NotAfter.ToUniversalTime() <= DateTime.UtcNow)
            {
                certificate.Dispose();
                throw new ArgumentException("REST Mock certificate must be current and include its private key.");
            }
            return certificate;
        }
        catch (CryptographicException)
        {
            // Do not expose malformed PEM, passwords or nested crypto messages.
            throw new InvalidOperationException("REST Mock certificate material is invalid.");
        }
        finally
        {
            Environment.SetEnvironmentVariable(CertificateVariable, null);
            Environment.SetEnvironmentVariable(KeyVariable, null);
            Environment.SetEnvironmentVariable("FOODTECH_MOCK_CERT_PASSWORD", null);
        }
    }
}
