import { describe, expect, it } from "vitest";
import { deriveWorkspaceIdentity, describeWorkspaceIdentity } from "./workspaceIdentity";

describe("deriveWorkspaceIdentity", () => {
  it("prefers the runner's git identity over the workspace path", () => {
    // The path's last segment is the worktree directory; the repo name the
    // runner reports is the one the user recognizes.
    expect(
      deriveWorkspaceIdentity(
        { repo: "omnigent", ref: "feat/login", detached: false, worktree: true },
        "/Users/alice/omnigent-worktrees/feat-login",
        null,
      ),
    ).toEqual({
      name: "omnigent",
      ref: "feat/login",
      detached: false,
      worktree: true,
      isRepo: true,
    });
  });

  it("carries a detached HEAD through as a commit, not a branch", () => {
    expect(
      deriveWorkspaceIdentity(
        { repo: "omnigent", ref: "a1b2c3d", detached: true, worktree: false },
        "/Users/alice/omnigent",
        null,
      ),
    ).toMatchObject({ ref: "a1b2c3d", detached: true });
  });

  it("backfills the branch from the session when the runner can't read HEAD", () => {
    // Runner reported the repo but no ref; the session's worktree branch is
    // the only ref known, and it is a branch — never a detached commit.
    expect(
      deriveWorkspaceIdentity(
        { repo: "omnigent", ref: null, detached: true, worktree: true },
        "/Users/alice/omnigent",
        "feat/login",
      ),
    ).toMatchObject({ ref: "feat/login", detached: false, worktree: true });
  });

  it("falls back to the workspace folder before the environment probe lands", () => {
    expect(deriveWorkspaceIdentity(undefined, "/Users/alice/myrepo", null)).toEqual({
      name: "myrepo",
      ref: null,
      detached: false,
      worktree: false,
      isRepo: false,
    });
  });

  it("treats a session branch as proof of a worktree in the fallback path", () => {
    // Session.gitBranch is only set for a server-created worktree.
    expect(
      deriveWorkspaceIdentity(null, "/Users/alice/myrepo-worktrees/feat-login", "feat/login"),
    ).toMatchObject({ name: "feat-login", ref: "feat/login", worktree: true, isRepo: true });
  });

  it("tolerates trailing separators and Windows paths", () => {
    expect(deriveWorkspaceIdentity(null, "/Users/alice/myrepo/", null)).toMatchObject({
      name: "myrepo",
    });
    expect(deriveWorkspaceIdentity(null, "C:\\Users\\alice\\myrepo", null)).toMatchObject({
      name: "myrepo",
    });
  });

  it("returns null when neither source knows where the session works", () => {
    expect(deriveWorkspaceIdentity(null, null, null)).toBeNull();
    expect(deriveWorkspaceIdentity(undefined, "   ", null)).toBeNull();
  });
});

describe("describeWorkspaceIdentity", () => {
  it("describes a repo checked out on a branch", () => {
    expect(
      describeWorkspaceIdentity({
        name: "omnigent",
        ref: "feat/login",
        detached: false,
        worktree: false,
        isRepo: true,
      }),
    ).toBe("Repository omnigent, checkout on branch feat/login");
  });

  it("names the worktree and the detached commit", () => {
    expect(
      describeWorkspaceIdentity({
        name: "omnigent",
        ref: "a1b2c3d",
        detached: true,
        worktree: true,
        isRepo: true,
      }),
    ).toBe("Repository omnigent, worktree detached at a1b2c3d");
  });

  it("describes a plain folder with no ref", () => {
    expect(
      describeWorkspaceIdentity({
        name: "scratch",
        ref: null,
        detached: false,
        worktree: false,
        isRepo: false,
      }),
    ).toBe("Folder scratch");
  });
});
