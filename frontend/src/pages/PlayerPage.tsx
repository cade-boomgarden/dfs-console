import { useParams } from "react-router-dom";
import { PlayerDetail } from "../components/PlayerDetail";

export default function PlayerPage() {
  const { slateId, playerId } = useParams();
  return <PlayerDetail playerId={Number(playerId)} slateId={slateId} />;
}
