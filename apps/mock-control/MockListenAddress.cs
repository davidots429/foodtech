using System.Net;
using System.Net.Sockets;

internal static class MockListenAddress
{
    // Explicit wildcard binding relies on the deployment's network policy.
    public static IPAddress Parse(string? value)
    {
        if (value is null) return IPAddress.Loopback;
        if (!IPAddress.TryParse(value, out var address) || address.AddressFamily != AddressFamily.InterNetwork)
            throw new ArgumentException("--bind-address must be an IPv4 address.");
        var bytes = address.GetAddressBytes();
        var allowed = address.Equals(IPAddress.Any) || bytes[0] == 127 || bytes[0] == 10 ||
                      (bytes[0] == 172 && bytes[1] is >= 16 and <= 31) ||
                      (bytes[0] == 192 && bytes[1] == 168);
        if (!allowed) throw new ArgumentException("--bind-address must be loopback, private IPv4 or 0.0.0.0.");
        return address;
    }
}
