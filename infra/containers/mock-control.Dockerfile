FROM mcr.microsoft.com/dotnet/sdk:10.0.402 AS builder
WORKDIR /src
COPY global.json ./
COPY apps/mock-control/MockControl.csproj apps/mock-control/packages.lock.json apps/mock-control/
RUN dotnet restore apps/mock-control/MockControl.csproj --locked-mode
COPY apps/mock-control/*.cs apps/mock-control/
RUN dotnet publish apps/mock-control/MockControl.csproj --configuration Release --no-restore --output /out

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=builder /out/ ./
COPY fixtures/ ./fixtures/
USER 10001:10001
ENTRYPOINT ["dotnet", "MockControl.dll"]
