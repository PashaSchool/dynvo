import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { Github, Eye, EyeOff } from "lucide-react";

export default function SignupPage() {
  const { signIn, isLoading } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  const handleGitHub = () => {
    signIn("github");
    setTimeout(() => navigate("/select-org"), 700);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    signIn("credentials");
    setTimeout(() => navigate("/select-org"), 700);
  };

  return (
    <div className="auth-page">
      <div className="auth-container">
        <div className="auth-logo">
          <div className="auth-logo-icon">FM</div>
          <div className="auth-logo-text">FeatureMap</div>
        </div>

        <div className="auth-card">
          <h1 className="auth-title">Create your account</h1>

          <button className="auth-social-btn auth-github-btn" onClick={handleGitHub} disabled={isLoading}>
            <Github size={18} />
            Sign up with GitHub
          </button>

          <div className="auth-divider">
            <span>or</span>
          </div>

          <form onSubmit={handleSubmit}>
            <div className="auth-field">
              <label className="auth-label">Full name</label>
              <input
                className="auth-input"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Jane Smith"
                autoComplete="name"
                autoFocus
              />
            </div>

            <div className="auth-field">
              <label className="auth-label">Email address</label>
              <input
                className="auth-input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                autoComplete="email"
              />
            </div>

            <div className="auth-field">
              <label className="auth-label">Password</label>
              <div className="auth-input-wrap">
                <input
                  className="auth-input"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Min 8 characters"
                  autoComplete="new-password"
                />
                <button
                  type="button"
                  className="auth-input-toggle"
                  onClick={() => setShowPassword(!showPassword)}
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              <div className="auth-hint">
                Must be at least 8 characters with one number
              </div>
            </div>

            <button className="auth-submit" type="submit" disabled={isLoading}>
              {isLoading ? "Creating account..." : "Create account"}
            </button>
          </form>

          <p className="auth-terms">
            By creating an account, you agree to the{" "}
            <a href="#">Terms of Service</a> and <a href="#">Privacy Policy</a>.
          </p>
        </div>

        <div className="auth-footer">
          Already have an account?{" "}
          <a className="auth-link" onClick={() => navigate("/login")}>
            Sign in
          </a>
        </div>
      </div>
    </div>
  );
}
