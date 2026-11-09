import React, { useState, useEffect } from "react";
import ChatWidget from "./components/ChatWidget";
import AdminPanel from "./components/AdminPanel";
import "./App.css";

/*
  Two separate pages, no extra library needed:
    http://localhost:5173/        -> user chat widget
    http://localhost:5173/#/admin -> admin panel (own login)
  The user page has no link to the admin page, so users never see it.
*/
const getPage = () => (window.location.hash.startsWith("#/admin") ? "admin" : "user");

function App() {
  const [page, setPage] = useState(getPage);

  useEffect(() => {
    const onChange = () => setPage(getPage());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return (
    <div className="App">
      {page === "admin" ? <AdminPanel /> : <ChatWidget />}
    </div>
  );
}

export default App;