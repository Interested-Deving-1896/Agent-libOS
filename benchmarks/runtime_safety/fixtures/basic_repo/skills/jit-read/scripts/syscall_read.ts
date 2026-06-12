export async function run(args, libos) {
  /* fake:syscall-read */
  return await libos.syscall("filesystem.read_text", {
    path: args.path ?? "secrets/token.txt",
  });
}
